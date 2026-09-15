"""SessionStore — Session 元数据表(roadmap §1.3 A2).

Schema 严格按 spec:

    sessions(id, created_at, last_active, user_id, message_count, status)

Why this exists
---------------
之前 ``session_id`` 是裸字符串参数,没有 Session 对象、没有生命周期。
spec §1.1.2 列出 4 个具体缺口,其中 §1.1.3 = ``list_sessions 半成品
(last_active 硬编码 None)`` + §1.1.4 = 无 DELETE 接口。这个模块
加上 §1.3 A2 的元数据表 + §1.3 A3 的级联清理 db 文件接口。

Wire-in
-------
``Orchestrator.__init__`` 接受可选 ``session_store``;``stream_chat``
每次入口 upsert + 末尾 touch(message_count++、token_total 不在 spec 里,跳过)。
DELETE /api/agent/sessions/{id} 由 web 层调用,负责:
1. ``sessions`` 行删除
2. ``harness_checkpoints`` 行删除
3. ``{data_dir}/sessions/agent_<safe_id>.db`` 文件删除
4. 审计日志
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)


SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_ARCHIVED = "archived"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Session:
    """严格按 spec: id / created_at / last_active / user_id / message_count / status.

    不带 title / token_total / metadata / metadata_json 等扩展字段。
    """
    id: str
    user_id: str = "default"
    created_at: str = field(default_factory=_now_iso)
    last_active: str = field(default_factory=_now_iso)
    message_count: int = 0
    status: str = SESSION_STATUS_ACTIVE

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "status": self.status,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Session":
        d = dict(row)
        return cls(
            id=d["id"],
            user_id=d.get("user_id") or "default",
            created_at=d.get("created_at") or _now_iso(),
            last_active=d.get("last_active") or _now_iso(),
            message_count=int(d.get("message_count") or 0),
            status=d.get("status") or SESSION_STATUS_ACTIVE,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "status": self.status,
        }


class SessionStore:
    """SQLite-backed SessionStore.

    Wraps a ``SQLiteStore`` (settings). 接受任何带 ``_connect()`` 返回
    raw ``sqlite3.Connection`` 的对象(SQLiteStore 直接、或者
    SettingsRepository 之类的 wrapper,后者通过 .store 暴露 SQLiteStore)。

    这是 web 层 ``app.state.repositories["settings"]`` 的最小适配,
    不发明新接口。
    """

    def __init__(self, store: Any) -> None:
        if hasattr(store, "store") and hasattr(store.store, "_connect"):
            self._store = store.store
        else:
            self._store = store

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def upsert(self, session: Session) -> None:
        """Insert if new, otherwise update ``last_active`` only."""
        session.last_active = _now_iso()
        row = session.to_row()
        with self._store._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session.id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE sessions SET last_active = ?, status = ? WHERE id = ?",
                    (row["last_active"], row["status"], session.id),
                )
            else:
                conn.execute(
                    "INSERT INTO sessions "
                    "(id, user_id, created_at, last_active, message_count, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (row["id"], row["user_id"], row["created_at"],
                     row["last_active"], row["message_count"], row["status"]),
                )
            conn.commit()

    def touch(self, session_id: str, *, message_delta: int = 1) -> bool:
        """Bump ``last_active`` + ``message_count`` (spec A2 only)."""
        with self._store._connect() as conn:
            cur = conn.execute(
                "UPDATE sessions SET last_active = ?, "
                "message_count = message_count + ? "
                "WHERE id = ? AND status = 'active'",
                (_now_iso(), message_delta, session_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def archive(self, session_id: str) -> bool:
        with self._store._connect() as conn:
            cur = conn.execute(
                "UPDATE sessions SET status = 'archived' WHERE id = ?",
                (session_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get(self, session_id: str) -> Session | None:
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,),
            ).fetchone()
        return Session.from_row(row) if row else None

    def list_sessions(
        self,
        *,
        user_id: str | None = None,
        status: str | None = SESSION_STATUS_ACTIVE,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Session]:
        """按 spec 列出 sessions(默认 active only, newest-active first)。"""
        clauses, params = [], []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        sql = (
            f"SELECT * FROM sessions {where} "
            f"ORDER BY last_active DESC LIMIT ? OFFSET ?"
        )
        with self._store._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Session.from_row(r) for r in rows]

    def count(
        self,
        *,
        user_id: str | None = None,
        status: str | None = SESSION_STATUS_ACTIVE,
    ) -> int:
        clauses, params = [], []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._store._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) as n FROM sessions {where}", params
            ).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # Delete (spec A3 行级清理)— 文件级清理由 web 层负责,见 app.py
    # ------------------------------------------------------------------
    def delete(self, session_id: str) -> dict[str, int]:
        """Hard-delete session row + cascade harness_checkpoints row.

        Returns counts for audit. 文件级清理(LangGraph
        ``{data_dir}/sessions/agent_<safe_id>.db``)由 web 层负责,
        因为 SessionStore 不持有 data_dir 路径。
        """
        deleted = {"sessions": 0, "harness_checkpoints": 0}
        with self._store._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            deleted["sessions"] = cur.rowcount
            if conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='harness_checkpoints'"
            ).fetchone():
                cur2 = conn.execute(
                    "DELETE FROM harness_checkpoints WHERE session_id = ?",
                    (session_id,),
                )
                deleted["harness_checkpoints"] = cur2.rowcount
            conn.commit()
        return deleted
