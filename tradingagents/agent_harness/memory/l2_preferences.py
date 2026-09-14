"""L2 preferences memory — user-level settings (v3 spec §3 l2_preferences.py).

Key/value store keyed by ``user_id`` (defaults to ``"default"``).
No TTL — preferences persist until explicitly deleted.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import MemoryEntry, MemoryLayer, MemoryScope


class UserPreferencesMemory(MemoryLayer):
    scope = MemoryScope.PREFERENCES

    def __init__(self, db_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._db_path = Path(db_path) if db_path else Path(".ta_cache") / "user_prefs.sqlite"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_prefs (
                    user_id TEXT NOT NULL DEFAULT 'default',
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, key)
                )
                """
            )
            conn.commit()

    def get(self, key: str, *, session_id: str | None = None) -> MemoryEntry | None:
        user_id = session_id or "default"
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT key, value, user_id, created_at, updated_at FROM user_prefs WHERE user_id = ? AND key = ?",
                (user_id, key),
            ).fetchone()
        if not row:
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
            session_id=row["user_id"],
            created_at=datetime.fromtimestamp(float(row["created_at"]), tz=timezone.utc),
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
        import time
        user_id = session_id or "default"
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_prefs(key, value, user_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (key, json.dumps(value, default=str), user_id, now, now),
            )
            conn.commit()
        return MemoryEntry(
            key=key, value=value, scope=self.scope, session_id=user_id,
        )

    def delete(self, key: str, *, session_id: str | None = None) -> bool:
        user_id = session_id or "default"
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM user_prefs WHERE user_id = ? AND key = ?",
                (user_id, key),
            )
            conn.commit()
        return cur.rowcount > 0

    def list(self, *, session_id: str | None = None, prefix: str | None = None) -> list[MemoryEntry]:
        user_id = session_id or "default"
        with self._lock, self._connect() as conn:
            if prefix:
                rows = conn.execute(
                    "SELECT key, value FROM user_prefs WHERE user_id = ? AND key LIKE ?",
                    (user_id, f"{prefix}%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT key, value FROM user_prefs WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
        import json
        out: list[MemoryEntry] = []
        for row in rows:
            try:
                value = json.loads(row["value"])
            except Exception:
                value = row["value"]
            out.append(MemoryEntry(key=row["key"], value=value, scope=self.scope, session_id=user_id))
        return out
