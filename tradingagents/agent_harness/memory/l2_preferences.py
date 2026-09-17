"""L2 preferences memory — user-level settings (v3 spec §3 l2_preferences.py).

Key/value store keyed by ``user_id`` (defaults to ``"default"``).
No TTL — preferences persist until explicitly deleted.

The :class:`MemoryLayer` ABC exposes ``session_id`` on every method for
API uniformity with L1/L3, but L2 ignores it: L1 is per-session, L3 is
per-agent, and L2 is per-USER. Passing ``session_id`` to an L2 method is a
bug — it implies the caller meant user_id and we kept silently treating
session_id as the storage key, which made every "preference" effectively
session-scoped.

Now we accept both ``user_id`` (preferred) and ``session_id`` (logged as
a deprecation warning when supplied). The storage key is always the
explicit ``user_id`` — never the session_id.
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

    def get(
        self,
        key: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> MemoryEntry | None:
        effective_user = self._resolve_user_id(
            user_id=user_id, session_id=session_id,
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT key, value, user_id, created_at, updated_at FROM user_prefs WHERE user_id = ? AND key = ?",
                (effective_user, key),
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
        user_id: str | None = None,
        ttl_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        import json
        import time
        effective_user = self._resolve_user_id(
            user_id=user_id, session_id=session_id,
        )
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_prefs(key, value, user_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (key, json.dumps(value, default=str), effective_user, now, now),
            )
            conn.commit()
        return MemoryEntry(
            key=key, value=value, scope=self.scope, session_id=effective_user,
        )

    def delete(
        self,
        key: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        effective_user = self._resolve_user_id(
            user_id=user_id, session_id=session_id,
        )
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM user_prefs WHERE user_id = ? AND key = ?",
                (effective_user, key),
            )
            conn.commit()
        return cur.rowcount > 0

    def list(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        prefix: str | None = None,
    ) -> list[MemoryEntry]:
        effective_user = self._resolve_user_id(
            user_id=user_id, session_id=session_id,
        )
        with self._lock, self._connect() as conn:
            if prefix:
                rows = conn.execute(
                    "SELECT key, value FROM user_prefs WHERE user_id = ? AND key LIKE ?",
                    (effective_user, f"{prefix}%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT key, value FROM user_prefs WHERE user_id = ?",
                    (effective_user,),
                ).fetchall()
        import json
        out: list[MemoryEntry] = []
        for row in rows:
            try:
                value = json.loads(row["value"])
            except Exception:
                value = row["value"]
            out.append(MemoryEntry(
                key=row["key"], value=value, scope=self.scope,
                session_id=effective_user,
            ))
        return out

    # ------------------------------------------------------------------
    # User-id resolution (single source of truth)
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_user_id(
        *,
        user_id: str | None,
        session_id: str | None,
    ) -> str:
        """Decide the storage key.

        ``user_id`` wins when explicitly supplied — that's the only way
        L2 ever makes sense. ``session_id`` is kept for ABC uniformity
        but logged as a deprecation: it almost certainly indicates the
        caller forgot to thread user_id through. We DON'T silently use
        session_id as user_id (the previous behaviour) because that
        turned user prefs into session prefs and made multi-session
        isolation impossible.
        """
        if user_id:
            return user_id
        if session_id:
            import warnings
            warnings.warn(
                "UserPreferencesMemory: 'session_id' is deprecated for L2; "
                "pass 'user_id' instead. Falling back to global 'default'.",
                DeprecationWarning,
                stacklevel=3,
            )
        return "default"
