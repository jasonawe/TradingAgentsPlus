"""L1 session memory — short-term per-session chat history (v3 spec §3 l1_session.py).

SQLite-backed, key format ``session:{session_id}:{key}``. Default TTL
24 hours; entries past their expiry are filtered on read.

W3-D6 R9: history 滚动归档 — 当超过 ``archive_threshold`` 时,
老消息通过 ``archive_callback`` 推到上层(EventLog 等),
不再静默截断丢失。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .base import MemoryEntry, MemoryLayer, MemoryScope

DEFAULT_TTL_SECONDS = 86_400  # 24h
DEFAULT_ARCHIVE_THRESHOLD = 200  # W3-D6 R9 default: 200 messages 触发滚动

LOGGER = logging.getLogger(__name__)


class SqliteSessionMemory(MemoryLayer):
    scope = MemoryScope.SESSION

    def __init__(
        self,
        db_path: Path | None = None,
        default_ttl: int = DEFAULT_TTL_SECONDS,
        archive_threshold: int = DEFAULT_ARCHIVE_THRESHOLD,
        archive_callback: Callable[[str, list], None] | None = None,
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
    # W3-D6 R9: history 滚动归档
    # ------------------------------------------------------------------
    def append_message(self, session_id: str, role: str, content: str) -> MemoryEntry:
        """Append a chat message; trigger rolling archive past threshold.

        W3-D6 R9: 不再静默截断到 200 条。

        - 有 ``archive_callback`` 时:超出 ``archive_threshold`` 的部分推到
          callback,history 保留最近 ``archive_threshold // 2`` 条(下限 50)。
          若 callback 抛错,数据不丢失,完整保留。
        - 无 ``archive_callback`` 时:走旧路径,history 截断到
          ``archive_threshold``(向后兼容)。
        - ``archive_threshold=0`` 时:完全禁用归档(老路径不限制)。
        """
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
        entry = self.get("history", session_id=session_id)
        return (entry.value if entry else []) or []
