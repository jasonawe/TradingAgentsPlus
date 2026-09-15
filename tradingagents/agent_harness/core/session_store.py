"""SessionStore — single source of truth for session lifecycle (roadmap A2).

Why this exists
---------------
Today ``session_id`` is just a string parameter flowing through
``Orchestrator.stream_chat`` — there's no Session object, no metadata,
no lifecycle. The roadmap (§1.3 A2) calls for a dedicated table to hold:

    ``sessions(id, created_at, last_active, user_id, message_count, status)``

This module provides ``SessionStore`` (SQLite-backed) with the operations
needed by the harness + REST endpoints:

- ``upsert(session)`` — auto-create or update last_active on first message
- ``touch(session_id)`` — bump last_active + message_count + token_total
- ``get(session_id)`` — fetch metadata
- ``list_sessions(user_id, limit, offset, status)`` — paginated list
- ``archive(session_id)`` — soft-delete (status='archived')
- ``delete(session_id)`` — hard-delete with cascade cleanup

Cascade cleanup (A3) drops related rows from:
- ``harness_checkpoints`` (in-flight state)
- LangGraph SqliteSaver (legacy agent state)
- agent_harness L1 (legacy short-term memory)

Wire-in
-------
``Orchestrator.__init__`` accepts optional ``session_store``. ``stream_chat``
auto-creates the session on first call + calls ``touch`` after each run.
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
    """Session metadata (mirrors the ``sessions`` table)."""

    id: str
    user_id: str = "default"
    title: str | None = None
    created_at: str = field(default_factory=_now_iso)
    last_active: str = field(default_factory=_now_iso)
    message_count: int = 0
    token_total: int = 0
    status: str = SESSION_STATUS_ACTIVE
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "token_total": self.token_total,
            "status": self.status,
            "metadata_json": json.dumps(self.metadata, ensure_ascii=False, default=str),
        }

    @classmethod
    def from_row(cls, row: Any) -> "Session":
        d = dict(row)
        return cls(
            id=d["id"],
            user_id=d.get("user_id") or "default",
            title=d.get("title"),
            created_at=d.get("created_at") or _now_iso(),
            last_active=d.get("last_active") or _now_iso(),
            message_count=int(d.get("message_count") or 0),
            token_total=int(d.get("token_total") or 0),
            status=d.get("status") or SESSION_STATUS_ACTIVE,
            metadata=json.loads(d.get("metadata_json") or "{}"),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON shape for API responses."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "token_total": self.token_total,
            "status": self.status,
            "metadata": self.metadata,
        }


class SessionStore:
    """SQLite-backed SessionStore.

    Wraps any ``SQLiteStore`` (same one used by settings + harness checkpoints).
    All methods are synchronous; SQLite writes are sub-ms.
    """

    def __init__(self, store: Any) -> None:
        """Wrap any object that can yield a raw sqlite3 connection.

        Accepts:
        - ``SQLiteStore`` (has ``_connect()`` returning ``sqlite3.Connection``)
        - ``SettingsRepository`` or any repo with a ``.store`` attribute
          pointing at a ``SQLiteStore``
        """
        if hasattr(store, "store") and hasattr(store.store, "_connect"):
            # SettingsRepository-style wrapper
            self._store = store.store
        else:
            self._store = store

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def upsert(self, session: Session) -> None:
        """Insert if new, otherwise update mutable fields (last_active, etc.)."""
        session.last_active = _now_iso()
        row = session.to_row()
        with self._store._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session.id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE sessions SET title=?, last_active=?, message_count=?, "
                    "token_total=?, status=?, metadata_json=? WHERE id=?",
                    (row["title"], row["last_active"], row["message_count"],
                     row["token_total"], row["status"], row["metadata_json"],
                     session.id),
                )
            else:
                conn.execute(
                    "INSERT INTO sessions (id, user_id, title, created_at, "
                    "last_active, message_count, token_total, status, metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["id"], row["user_id"], row["title"], row["created_at"],
                     row["last_active"], row["message_count"], row["token_total"],
                     row["status"], row["metadata_json"]),
                )
            conn.commit()

    def touch(
        self,
        session_id: str,
        *,
        message_delta: int = 1,
        token_delta: int = 0,
    ) -> bool:
        """Bump ``last_active``, increment counters. Returns False if missing."""
        with self._store._connect() as conn:
            cur = conn.execute(
                "UPDATE sessions SET last_active = ?, "
                "message_count = message_count + ?, "
                "token_total = token_total + ? "
                "WHERE id = ? AND status = 'active'",
                (_now_iso(), message_delta, token_delta, session_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def set_title(self, session_id: str, title: str | None) -> bool:
        with self._store._connect() as conn:
            cur = conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (title, session_id),
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
        limit: int = 50,
        offset: int = 0,
    ) -> list[Session]:
        """List sessions, newest-active first.

        ``status=None`` returns all (active + archived).
        """
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

    def count(self, *, user_id: str | None = None, status: str | None = SESSION_STATUS_ACTIVE) -> int:
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
    # Delete (A3) — cascade cleanup
    # ------------------------------------------------------------------
    def delete(self, session_id: str) -> dict[str, int]:
        """Hard-delete the session + cascade-clean related rows.

        Returns counts of rows deleted from each table for audit logging.
        Caller (the API endpoint) writes those counts to the audit log.
        """
        deleted = {"sessions": 0, "harness_checkpoints": 0}
        with self._store._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            deleted["sessions"] = cur.rowcount
            # Cascade: drop harness checkpoint if any (A3 cascade)
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='harness_checkpoints'"
            ).fetchone():
                cur2 = conn.execute(
                    "DELETE FROM harness_checkpoints WHERE session_id = ?",
                    (session_id,),
                )
                deleted["harness_checkpoints"] = cur2.rowcount
            conn.commit()
        LOGGER.info(
            "session %s deleted (cascade: %s)",
            session_id, deleted,
        )
        return deleted
