"""L1 session memory — short-term per-session chat history (v3 spec §3 l1_session.py).

SQLite-backed, key format ``session:{session_id}:{key}``. Default TTL
24 hours; entries past their expiry are filtered on read.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .base import MemoryEntry, MemoryLayer, MemoryScope

DEFAULT_TTL_SECONDS = 86_400  # 24h


class SqliteSessionMemory(MemoryLayer):
    scope = MemoryScope.SESSION

    def __init__(
        self,
        db_path: Path | None = None,
        default_ttl: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._db_path = Path(db_path) if db_path else Path(".ta_cache") / "session_memory.sqlite"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl
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

    def append_message(self, session_id: str, role: str, content: str) -> MemoryEntry:
        """Convenience: append a chat message to the session's history."""
        history = self.get("history", session_id=session_id)
        msgs = (history.value if history else []) or []
        msgs.append({"role": role, "content": content, "ts": time.time()})
        # Keep last 200 messages per session.
        msgs = msgs[-200:]
        return self.set("history", msgs, session_id=session_id)

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        entry = self.get("history", session_id=session_id)
        return (entry.value if entry else []) or []
