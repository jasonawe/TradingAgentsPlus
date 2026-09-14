"""L3 references memory — agent-level knowledge base (v3 spec §3 l3_references.py).

Cross-session knowledge cache (e.g. recently quoted symbols, watchlist
metadata, fetched fundamentals). Keyed by ``kind`` (``quotes``,
``fundamentals``, ``news``) + ``symbol`` — queries can hit any
combination.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import MemoryEntry, MemoryLayer, MemoryScope


class AgentReferencesMemory(MemoryLayer):
    scope = MemoryScope.REFERENCES

    def __init__(self, db_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._db_path = Path(db_path) if db_path else Path(".ta_cache") / "agent_refs.sqlite"
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
                CREATE TABLE IF NOT EXISTS agent_refs (
                    kind TEXT NOT NULL,
                    ref_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    session_id TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    PRIMARY KEY (kind, ref_key)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_refs_kind ON agent_refs(kind)")
            conn.commit()

    def get(self, key: str, *, session_id: str | None = None) -> MemoryEntry | None:
        kind, _, ref_key = key.partition(":")
        if not ref_key:
            kind, ref_key = "default", key
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT kind, ref_key, value, session_id, created_at, expires_at FROM agent_refs WHERE kind = ? AND ref_key = ?",
                (kind, ref_key),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] and float(row["expires_at"]) < time.time():
            self._delete_row(kind, ref_key)
            return None
        try:
            value = json.loads(row["value"])
        except Exception:
            value = row["value"]
        return MemoryEntry(
            key=f"{kind}:{ref_key}",
            value=value,
            scope=self.scope,
            session_id=row["session_id"],
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
        kind, _, ref_key = key.partition(":")
        if not ref_key:
            kind, ref_key = "default", key
        expires_at = (time.time() + ttl_seconds) if ttl_seconds else None
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_refs(kind, ref_key, value, session_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, ref_key, json.dumps(value, default=str), session_id, time.time(), expires_at),
            )
            conn.commit()
        return MemoryEntry(
            key=f"{kind}:{ref_key}", value=value, scope=self.scope, session_id=session_id,
        )

    def delete(self, key: str, *, session_id: str | None = None) -> bool:
        kind, _, ref_key = key.partition(":")
        if not ref_key:
            kind, ref_key = "default", key
        return self._delete_row(kind, ref_key)

    def _delete_row(self, kind: str, ref_key: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM agent_refs WHERE kind = ? AND ref_key = ?",
                (kind, ref_key),
            )
            conn.commit()
        return cur.rowcount > 0

    def list(self, *, session_id: str | None = None, prefix: str | None = None) -> list[MemoryEntry]:
        if not prefix:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT kind, ref_key, value FROM agent_refs ORDER BY kind, ref_key"
                ).fetchall()
        else:
            kind, _, ref_key_prefix = prefix.partition(":")
            if not ref_key_prefix:
                with self._lock, self._connect() as conn:
                    rows = conn.execute(
                        "SELECT kind, ref_key, value FROM agent_refs WHERE kind = ?",
                        (kind,),
                    ).fetchall()
            else:
                with self._lock, self._connect() as conn:
                    rows = conn.execute(
                        "SELECT kind, ref_key, value FROM agent_refs WHERE kind = ? AND ref_key LIKE ?",
                        (kind, f"{ref_key_prefix}%"),
                    ).fetchall()
        out: list[MemoryEntry] = []
        for row in rows:
            try:
                value = json.loads(row["value"])
            except Exception:
                value = row["value"]
            out.append(MemoryEntry(
                key=f"{row['kind']}:{row['ref_key']}",
                value=value,
                scope=self.scope,
            ))
        return out
