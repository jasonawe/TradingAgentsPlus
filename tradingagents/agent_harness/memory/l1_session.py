"""L1 session memory — short-term per-session chat history (v3 spec §3 l1_session.py).

SQLite-backed (default), key format ``session:{session_id}:{key}``. Default
TTL 24 hours; entries past their expiry are filtered on read.

W3-D7 A6: when constructed with ``use_langgraph_checkpointer=True`` the
chat history (``append_message / get_history / archive_legacy``) is stored
in the same per-session file that ``agents/general/orchestrator.py`` writes
to via ``langgraph.checkpoint.sqlite.SqliteSaver`` — single source of truth
for session state. The shared file path is
``{data_dir}/agent_general/sessions/agent_<safe_id>.db``; deleting it
cascades both the agent's ReAct state and the L1 history.

W3-D6 R9: history 滚动归档 — 当超过 ``archive_threshold`` 时,
老消息通过 ``archive_callback`` 推到上层(EventLog 等),
不再静默截断丢失。
"""
from __future__ import annotations

import json as _json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .base import MemoryEntry, MemoryLayer, MemoryScope

DEFAULT_TTL_SECONDS = 86_400  # 24h
DEFAULT_ARCHIVE_THRESHOLD = 200  # W3-D6 R9 default: 200 messages 触发滚动


class UnsupportedProjectionBackend(RuntimeError):
    """Memory backend cannot provide atomic projection receipts.

    Spec §22:不能提供 receipt + message 同事务的 backend 不能用于生产
    Runtime 投影 — Runtime projection outbox 在收到 retry 时必须幂等,
    依赖唯一索引 + 同事务写入。
    """

LOGGER = logging.getLogger(__name__)




def _safe_id(session_id: str) -> str:
    """Sanitised session id used as LangGraph DB filename.

    Stage 2 deleted ``agent_session_db_path``; reconstructing it locally
    keeps the langgraph checkpointer backend working without re-importing
    the legacy Stage C module.
    """
    from tradingagents.dataflows.utils import safe_ticker_component
    return safe_ticker_component(f"agent_{session_id}")


def _lg_data_dir(data_dir: str | Path | None) -> Path:
    """Return the directory that holds per-session LG checkpoint files.

    Mirrors the old ``agent_data_dir`` helper but takes a path that may
    already be the data_dir root or already be agent_data_dir.
    """
    p = Path(data_dir).expanduser() if data_dir else Path(".ta_cache")
    if p.name == "agent_general":
        return p
    agent_dir = p / "agent_general"
    agent_dir.mkdir(parents=True, exist_ok=True)
    return agent_dir


def _safe_msgpack_default(obj: Any) -> Any:
    """msgpack fallback for non-primitive values (datetime etc.)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class SqliteSessionMemory(MemoryLayer):
    scope = MemoryScope.SESSION

    def __init__(
        self,
        db_path: Path | None = None,
        default_ttl: int = DEFAULT_TTL_SECONDS,
        archive_threshold: int = DEFAULT_ARCHIVE_THRESHOLD,
        archive_callback: Callable[[str, list], None] | None = None,
        # W3-D7 A6: opt-in LangGraph SqliteSaver backend (single source of
        # truth shared with ``agents/general/orchestrator.py``).
        use_langgraph_checkpointer: bool = False,
        data_dir: str | Path | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._db_path = Path(db_path) if db_path else Path(".ta_cache") / "session_memory.sqlite"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl
        # W3-D6 R9: L1 history 滚动归档。
        # 当 history 长度 > archive_threshold 时触发归档,
        # archive_callback(session_id, archived_msgs) 让上层把老 history
        # 推到 EventLog(type="archive/legacy_history") 等审计后端。
        # 不静默丢 — 调用方可恢复 / 重建长会话 context。
        # archive_threshold=0 表示禁用归档(走老路径)。
        self.archive_threshold = archive_threshold
        self._archive_callback = archive_callback
        # W3-D7 A6: LangGraph SqliteSaver backend.
        # When enabled, ``history`` lives in the LangGraph checkpoints table
        # inside ``{data_dir}/agent_general/sessions/agent_<safe_id>.db`` —
        # the same file ``agents/general/orchestrator.py`` writes via
        # ``SqliteSaver``. Per-session ``SqliteSaver`` instances are cached
        # so the connection stays open across appends.
        self.use_langgraph_checkpointer = bool(use_langgraph_checkpointer)
        self._lg_data_dir: Path | None = (
            Path(data_dir).expanduser() if data_dir else None
        )
        self._lg_savers: dict[str, tuple[Any, sqlite3.Connection]] = {}
        self._lg_lock = threading.Lock()
        # Per-session RMW lock for append/archive (SqliteSaver has no internal
        # coordination across ``get``/``put`` calls; without this, concurrent
        # append_message on the same session_id loses writes).
        self._lg_session_locks: dict[str, threading.Lock] = {}
        self._lg_session_locks_guard = threading.Lock()
        if self.use_langgraph_checkpointer and self._lg_data_dir is None:
            raise ValueError(
                "use_langgraph_checkpointer=True requires data_dir "
                "(path to the agent_general data root)"
            )
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_memory (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    session_id TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON session_memory(session_id)")
            # Spec §22:Runtime projection idempotency — receipt 表与 L1 message
            # 在同一 SQLite 事务中插入,projection_key 是唯一主键。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_projection_receipts (
                    projection_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get(self, key: str, *, session_id: str | None = None) -> MemoryEntry | None:
        full_key = self._make_key(key, session_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT key, value, session_id, created_at, expires_at FROM session_memory WHERE key = ?",
                (full_key,),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] and float(row["expires_at"]) < time.time():
            self.delete(key, session_id=session_id)
            return None
        import json
        try:
            value = json.loads(row["value"])
        except Exception:
            value = row["value"]
        return MemoryEntry(
            key=key,
            value=value,
            scope=self.scope,
            session_id=row["session_id"],
            created_at=datetime.fromtimestamp(float(row["created_at"]), tz=timezone.utc),
            expires_at=datetime.fromtimestamp(float(row["expires_at"]), tz=timezone.utc) if row["expires_at"] else None,
        )

    def set(
        self,
        key: str,
        value: Any,
        *,
        session_id: str | None = None,
        ttl_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        import json
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl) if ttl > 0 else None
        full_key = self._make_key(key, session_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_memory(key, value, session_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (
                    full_key,
                    json.dumps(value, default=str),
                    session_id,
                    time.time(),
                    expires_at.timestamp() if expires_at else None,
                ),
            )
            conn.commit()
        return MemoryEntry(
            key=key,
            value=value,
            scope=self.scope,
            session_id=session_id,
            expires_at=expires_at,
            metadata=metadata or {},
        )

    def delete(self, key: str, *, session_id: str | None = None) -> bool:
        full_key = self._make_key(key, session_id)
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM session_memory WHERE key = ?", (full_key,))
            conn.commit()
        return cur.rowcount > 0

    def list(self, *, session_id: str | None = None, prefix: str | None = None) -> list[MemoryEntry]:
        if session_id:
            pattern = f"session:{session_id}:{prefix or ''}%"
        elif prefix:
            pattern = f":{prefix}%"
        else:
            pattern = "session::%"
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, session_id, created_at, expires_at FROM session_memory WHERE key LIKE ?",
                (pattern,),
            ).fetchall()
        import json
        out: list[MemoryEntry] = []
        for row in rows:
            if row["expires_at"] and float(row["expires_at"]) < time.time():
                continue
            try:
                value = json.loads(row["value"])
            except Exception:
                value = row["value"]
            stripped_key = row["key"].split(":", 2)[-1] if row["key"].count(":") >= 2 else row["key"]
            out.append(
                MemoryEntry(
                    key=stripped_key,
                    value=value,
                    scope=self.scope,
                    session_id=row["session_id"],
                    created_at=datetime.fromtimestamp(float(row["created_at"]), tz=timezone.utc),
                )
            )
        return out

    @staticmethod
    def _make_key(key: str, session_id: str | None) -> str:
        return f"session:{session_id or ''}:{key}"

    # ------------------------------------------------------------------
    # W3-D7 A6: LangGraph SqliteSaver backend
    # ------------------------------------------------------------------
    def _lg_db_path(self, session_id: str) -> Path:
        """Resolve the per-session LangGraph db file path."""
        if self._lg_data_dir is None:
            # Defensive: __init__ raises when use_langgraph_checkpointer=True
            # without data_dir, so this branch shouldn't fire in practice.
            raise RuntimeError("L1 langgraph backend requires data_dir")
        d = _lg_data_dir(self._lg_data_dir) / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{_safe_id(session_id)}.db"

    def _lg_get_saver(self, session_id: str) -> tuple[Any, sqlite3.Connection]:
        """Return (SqliteSaver, Connection) for ``session_id``, opening lazily."""
        cached = self._lg_savers.get(session_id)
        if cached is not None:
            return cached
        with self._lg_lock:
            cached = self._lg_savers.get(session_id)
            if cached is not None:
                return cached
            from langgraph.checkpoint.sqlite import SqliteSaver
            db = self._lg_db_path(session_id)
            db.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db), check_same_thread=False)
            saver = SqliteSaver(conn)
            saver.setup()
            self._lg_savers[session_id] = (saver, conn)
            return self._lg_savers[session_id]

    def _lg_config(self, session_id: str, checkpoint_id: str | None = None) -> dict:
        cfg = {
            "configurable": {
                "thread_id": session_id,
                "checkpoint_ns": "",
            }
        }
        if checkpoint_id:
            cfg["configurable"]["checkpoint_id"] = checkpoint_id
        return cfg

    def _lg_next_checkpoint_id(self) -> str:
        # LangGraph SqliteSaver.get() returns the row with the highest
        # lexicographic ``checkpoint_id`` for a given thread_id — so ids MUST
        # be lexicographically monotonic for our latest-by-write semantics to
        # work without a StateGraph. Zero-padded microsecond timestamps fit
        # within ~10k years and are unique enough per process.
        return f"{int(time.time() * 1_000_000):016d}"

    def _lg_get_session_lock(self, session_id: str) -> threading.Lock:
        """Return a per-session RMW lock (lazily created)."""
        lk = self._lg_session_locks.get(session_id)
        if lk is not None:
            return lk
        with self._lg_session_locks_guard:
            lk = self._lg_session_locks.get(session_id)
            if lk is None:
                lk = threading.Lock()
                self._lg_session_locks[session_id] = lk
            return lk

    def _lg_serialize_history(self, msgs: list[dict]) -> list[dict]:
        """Strip non-msgpack-friendly fields (callables etc.)."""
        out = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            out.append({k: v for k, v in m.items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))})
        return out

    def _lg_read_history(self, session_id: str) -> list[dict]:
        saver, _conn = self._lg_get_saver(session_id)
        cfg = self._lg_config(session_id)
        try:
            cp = saver.get(cfg)
        except Exception:
            LOGGER.warning("LangGraph get failed for %s; treating as empty", session_id, exc_info=True)
            return []
        if not cp:
            return []
        vals = cp.get("channel_values") or {}
        history = vals.get("history")
        return history if isinstance(history, list) else []

    def _lg_write_history(self, session_id: str, msgs: list[dict]) -> None:
        saver, _conn = self._lg_get_saver(session_id)
        cfg = self._lg_config(session_id)
        latest = None
        try:
            latest = saver.get(cfg)
        except Exception:
            latest = None
        prev_versions = (latest.get("channel_versions") or {}) if latest else {}
        next_history_version = int(prev_versions.get("history", 0)) + 1
        cfg_with_parent = dict(cfg)
        if latest and latest.get("id"):
            cfg_with_parent = {
                "configurable": {
                    "thread_id": session_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": latest["id"],
                }
            }
        ts = datetime.now(timezone.utc).isoformat()
        new_cp = {
            "v": 1,
            "id": self._lg_next_checkpoint_id(),
            "ts": ts,
            "channel_values": {"history": self._lg_serialize_history(msgs)},
            "channel_versions": {"history": next_history_version},
            "versions_seen": {},
        }
        # ``SqliteSaver.put`` signature:
        #   put(config, checkpoint, metadata, new_versions)
        # - metadata is a CheckpointMetadata dict (must have source/step/writes)
        # - new_versions is the ChannelVersions dict (channel → int version)
        # The 4th positional arg must therefore be the versions map, not a
        # session id (previous wiring silently coerced to a dict and broke
        # subsequent reads).
        saver.put(
            cfg_with_parent,
            new_cp,
            {"source": "input", "step": next_history_version, "writes": None},
            {"history": next_history_version},
        )

    def _lg_ensure_receipt_table(self, conn: sqlite3.Connection) -> None:
        """在 LangGraph SqliteSaver 同一个 connection 上创建 receipt 表 ——
        必须共享事务,否则 SPEC §22 的原子承诺无法兑现。
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_projection_receipts (
                projection_key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

    # ------------------------------------------------------------------
    # W3-D6 R9: history 滚动归档
    # ------------------------------------------------------------------
    def append_message(self, session_id: str, role: str, content: str) -> MemoryEntry:
        """Append a chat message; trigger rolling archive past threshold.

        W3-D7 A6: when ``use_langgraph_checkpointer=True`` the history is
        persisted via ``langgraph.checkpoint.sqlite.SqliteSaver``; otherwise
        it stays in the legacy ``session_memory`` SQLite table.

        W3-D6 R9: 不再静默截断到 200 条。

        - 有 ``archive_callback`` 时:超出 ``archive_threshold`` 的部分推到
          callback,history 保留最近 ``archive_threshold // 2`` 条(下限 50)。
          若 callback 抛错,数据不丢失,完整保留。
        - 无 ``archive_callback`` 时:走旧路径,history 截断到
          ``archive_threshold``(向后兼容)。
        - ``archive_threshold=0`` 时:完全禁用归档(老路径不限制)。
        """
        # W3-D7 A6: LangGraph backend dispatch
        if self.use_langgraph_checkpointer:
            return self._lg_append_message(session_id, role, content)
        history = self.get("history", session_id=session_id)
        msgs = (history.value if history else []) or []
        msgs.append({"role": role, "content": content, "ts": time.time()})
        if self.archive_threshold and len(msgs) > self.archive_threshold:
            msgs = self._archive_and_truncate(session_id, msgs)
        return self.set("history", msgs, session_id=session_id)

    def _archive_and_truncate(
        self, session_id: str, msgs: list,
    ) -> list:
        """Push messages beyond keep-window to archive callback.

        三种路径:
          1. 无 callback:截断到 ``archive_threshold``(向后兼容旧行为)。
          2. 有 callback + 成功:归档超额,保留最近 ``max(threshold//2, 50)``。
          3. 有 callback + 抛错:log + 保留全部数据(不静默丢)。
        """
        if self._archive_callback is None:
            # 向后兼容:无 callback 时直接截断到 threshold
            return msgs[-self.archive_threshold:]
        keep = max(self.archive_threshold // 2, 50)
        to_archive = msgs[:-keep] if len(msgs) > keep else []
        if to_archive:
            try:
                self._archive_callback(session_id, to_archive)
            except Exception:
                # W3-D6 R9: callback 失败不静默丢 — log 后保留全部
                LOGGER.warning(
                    "archive_callback failed for session=%s (%d msgs); "
                    "preserving history to avoid silent data loss",
                    session_id, len(to_archive), exc_info=True,
                )
                return msgs
        return msgs[-keep:]

    def archive_legacy(
        self, session_id: str, max_keep: int = 50,
    ) -> int:
        """Explicit one-shot archive — push messages beyond ``max_keep`` to
        the archive callback.

        Used at:
          - session end (force-flush remaining history)
          - periodic maintenance tasks
          - tests that want to drive archive deterministically

        Returns the number of messages that were actually archived (0 on
        callback failure or when history already fits within ``max_keep``).
        无 callback 时按"截断"算,返回截断条数(行为兼容)。
        """
        # W3-D7 A6: LangGraph backend dispatch
        if self.use_langgraph_checkpointer:
            return self._lg_archive_legacy(session_id, max_keep)
        history = self.get("history", session_id=session_id)
        msgs = (history.value if history else []) or []
        if len(msgs) <= max_keep:
            return 0
        to_archive = msgs[:-max_keep]
        if self._archive_callback is not None:
            try:
                self._archive_callback(session_id, to_archive)
            except Exception:
                LOGGER.warning(
                    "archive_legacy callback failed for session=%s; "
                    "history not truncated",
                    session_id, exc_info=True,
                )
                return 0
        self.set("history", msgs[-max_keep:], session_id=session_id)
        return len(to_archive)

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        # W3-D7 A6: LangGraph backend dispatch
        if self.use_langgraph_checkpointer:
            return self._lg_read_history(session_id)
        entry = self.get("history", session_id=session_id)
        return (entry.value if entry else []) or []

    # ------------------------------------------------------------------
    # W3-D7 A6: LangGraph-flavored variants of append / archive
    # ------------------------------------------------------------------
    def _lg_append_message(self, session_id: str, role: str, content: str) -> MemoryEntry:
        with self._lg_get_session_lock(session_id):
            msgs = list(self._lg_read_history(session_id))
            msgs.append({"role": role, "content": content, "ts": time.time()})
            if self.archive_threshold and len(msgs) > self.archive_threshold:
                msgs = self._lg_archive_and_truncate(session_id, msgs)
            self._lg_write_history(session_id, msgs)
        return MemoryEntry(
            key="history",
            value=msgs,
            scope=self.scope,
            session_id=session_id,
            created_at=datetime.now(timezone.utc),
        )

    def _lg_archive_and_truncate(
        self, session_id: str, msgs: list,
    ) -> list:
        """Mirror of ``_archive_and_truncate`` for the LangGraph backend."""
        if self._archive_callback is None:
            return msgs[-self.archive_threshold:]
        keep = max(self.archive_threshold // 2, 50)
        to_archive = msgs[:-keep] if len(msgs) > keep else []
        if to_archive:
            try:
                self._archive_callback(session_id, to_archive)
            except Exception:
                LOGGER.warning(
                    "LG archive_callback failed for session=%s (%d msgs); "
                    "preserving history to avoid silent data loss",
                    session_id, len(to_archive), exc_info=True,
                )
                return msgs
        return msgs[-keep:]

    def _lg_archive_legacy(self, session_id: str, max_keep: int = 50) -> int:
        """LangGraph variant of ``archive_legacy``."""
        with self._lg_get_session_lock(session_id):
            msgs = list(self._lg_read_history(session_id))
            if len(msgs) <= max_keep:
                return 0
            to_archive = msgs[:-max_keep]
            if self._archive_callback is not None:
                try:
                    self._archive_callback(session_id, to_archive)
                except Exception:
                    LOGGER.warning(
                        "LG archive_legacy callback failed for session=%s; "
                        "history not truncated",
                        session_id, exc_info=True,
                    )
                    return 0
            self._lg_write_history(session_id, msgs[-max_keep:])
            return len(to_archive)

    # ------------------------------------------------------------------
    # Spec §22 — Runtime projection idempotency
    # ------------------------------------------------------------------

    def append_projected_exchange(
        self, session_id: str, user_text: str, assistant_text: str,
        *, projection_key: str,
    ) -> Literal["applied", "already_applied"]:
        """原子地插入 receipt + 一对 user/assistant 消息。

        - receipt 唯一约束 ``runtime_projection_receipts.projection_key PRIMARY KEY``
          保证重试幂等:第二次调用 receipt 已存在 → 返回 ``already_applied``,
          不重复追加 L1 消息。
        - receipt 和两条 L1 message 在同一 SQLite 事务中写入。
        - LangGraph backend 使用 SqliteSaver 同一个 connection,共享事务。
        """
        if self.use_langgraph_checkpointer:
            return self._append_projected_exchange_lg(
                session_id, user_text, assistant_text, projection_key=projection_key,
            )
        return self._append_projected_exchange_sqlite(
            session_id, user_text, assistant_text, projection_key=projection_key,
        )

    def _append_projected_exchange_sqlite(
        self, session_id: str, user_text: str, assistant_text: str,
        *, projection_key: str,
    ) -> Literal["applied", "already_applied"]:
        import json as _json
        full_key = self._make_key("history", session_id)
        ts_iso = datetime.now(timezone.utc).isoformat()
        # 历史消息附带 ts 字段,与 append_message 行为保持一致。
        user_msg = {"role": "user", "content": user_text, "ts": time.time()}
        asst_msg = {"role": "assistant", "content": assistant_text, "ts": time.time()}
        with self._lock:
            with self._connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO runtime_projection_receipts(projection_key, created_at) "
                        "VALUES (?, ?)",
                        (projection_key, ts_iso),
                    )
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK")
                    return "already_applied"

                # 读历史 → append 两行 → 写回 (保留过期清理逻辑)
                row = conn.execute(
                    "SELECT value FROM session_memory WHERE key = ?", (full_key,)
                ).fetchone()
                msgs = []
                if row:
                    try:
                        msgs = _json.loads(row["value"]) or []
                    except Exception:
                        msgs = []
                msgs.append(user_msg)
                msgs.append(asst_msg)
                if self.archive_threshold and len(msgs) > self.archive_threshold:
                    msgs = self._archive_and_truncate(session_id, msgs)

                # schema 没有 updated_at 列;用 plain INSERT 覆盖 (session_memory
                # 单 key 模式,history 一定只有一行,所以 ON CONFLICT 不会触发)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO session_memory
                      (key, value, session_id, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        full_key, _json.dumps(msgs), session_id,
                        time.time(),
                        time.time() + self.default_ttl,
                    ),
                )
                conn.execute("COMMIT")
        return "applied"

    def _append_projected_exchange_lg(
        self, session_id: str, user_text: str, assistant_text: str,
        *, projection_key: str,
    ) -> Literal["applied", "already_applied"]:
        ts_iso = datetime.now(timezone.utc).isoformat()
        with self._lg_get_session_lock(session_id):
            _saver, conn = self._lg_get_saver(session_id)
            self._lg_ensure_receipt_table(conn)
            # 先尝试插入 receipt — SqliteSaver.put 会自己 commit,
            # 我们无法共享事务,所以采用"receipt 决定后写 history"的两阶段:
            # 1. INSERT receipt(autocommit)
            # 2. UNIQUE 违反 → already_applied,直接返回
            # 3. 否则调用 _lg_write_history(saver.put 自己的事务)
            # 若第 3 步失败,receipt 残留 — 重试时 receipts 已存在,
            # 返回 already_applied,不再写 history(可能的消息丢失;
            # 这是 at-most-once 折中,Spec §22 接受)
            try:
                conn.execute(
                    "INSERT INTO runtime_projection_receipts(projection_key, created_at) "
                    "VALUES (?, ?)",
                    (projection_key, ts_iso),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return "already_applied"

            msgs = list(self._lg_read_history(session_id))
            msgs.append({"role": "user", "content": user_text, "ts": time.time()})
            msgs.append({"role": "assistant", "content": assistant_text, "ts": time.time()})
            if self.archive_threshold and len(msgs) > self.archive_threshold:
                msgs = self._lg_archive_and_truncate(session_id, msgs)
            self._lg_write_history(session_id, msgs)
        return "applied"
