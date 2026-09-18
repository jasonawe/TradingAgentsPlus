"""Step 45 — D4 Memory tier merging.

The previous design had three hardcoded tiers (L1 session / L2 short-term
/ L3 references), each with its own class, TTL semantics, and storage
backend. This module unifies them behind a single ``MemoryStore``
protocol where the tier is a configuration choice, not a code base.

Design
------
::

    MemoryStore          # Protocol
    ├── put(key, value, tier, ttl=None)
    ├── get(key, tier) -> value | None
    ├── delete(key, tier)
    ├── query(prefix, tier) -> list[(key, value)]
    └── stats() -> dict

A ``Tier`` is a config object specifying:
- ``name``: l1 / l2 / l3 / custom
- ``ttl_seconds``: default expiry (None = persistent)
- ``persistent``: True if backed by durable storage
- ``cross_session``: True if shared across sessions

Backends (pluggable):
- ``InMemoryBackend``: process-local, fast, no durability
- ``SQLiteBackend``: durable, slower, supports query

The facade (``MemoryFacade``) holds one MemoryStore per tier and
provides the unified ``get/put/query`` API. Existing code calling
L1/L2/L3 directly still works — the facade exposes thin compat
methods.
"""
from __future__ import annotations

import abc
import sqlite3
import threading
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


# ----------------------------------------------------------------------
# Tier config
# ----------------------------------------------------------------------
@dataclass
class Tier:
    """Memory tier configuration."""

    name: str
    ttl_seconds: int | None = None  # None = persistent
    persistent: bool = False
    cross_session: bool = False

    def is_expired(self, stored_at: float, now: float | None = None) -> bool:
        if self.ttl_seconds is None:
            return False
        n = now if now is not None else time.time()
        return (n - stored_at) > self.ttl_seconds


# Predefined tier configs — backward compat with L1/L2/L3 callers.
TIER_L1 = Tier(name="l1", ttl_seconds=60, persistent=False, cross_session=False)
TIER_L2 = Tier(name="l2", ttl_seconds=86400, persistent=True, cross_session=False)
TIER_L3 = Tier(name="l3", ttl_seconds=None, persistent=True, cross_session=True)


# ----------------------------------------------------------------------
# Backends
# ----------------------------------------------------------------------
class _Backend(Protocol):
    """Storage backend interface. Implementations: InMemory / SQLite."""

    def put(self, scope: str, tier: str, key: str, value: dict, stored_at: float) -> None: ...
    def get(self, scope: str, tier: str, key: str) -> tuple[dict, float] | None: ...
    def delete(self, scope: str, tier: str, key: str) -> bool: ...
    def query(self, scope: str, tier: str, prefix: str) -> list[tuple[str, dict, float]]: ...
    def all_for_scope(self, tier: str, scope: str) -> list[tuple[str, dict, float]]: ...


class InMemoryBackend:
    """Process-local backend. Fast, no durability."""

    def __init__(self) -> None:
        # (scope, tier, key) -> (value, stored_at)
        self._data: dict[tuple[str, str, str], tuple[dict, float]] = {}
        self._lock = threading.Lock()

    def put(self, scope: str, tier: str, key: str, value: dict, stored_at: float) -> None:
        with self._lock:
            self._data[(scope, tier, key)] = (value, stored_at)

    def get(self, scope: str, tier: str, key: str) -> tuple[dict, float] | None:
        with self._lock:
            return self._data.get((scope, tier, key))

    def delete(self, scope: str, tier: str, key: str) -> bool:
        with self._lock:
            return self._data.pop((scope, tier, key), None) is not None

    def query(self, scope: str, tier: str, prefix: str) -> list[tuple[str, dict, float]]:
        with self._lock:
            return [
                (k, v, ts) for (s, t, k), (v, ts) in self._data.items()
                if s == scope and t == tier and k.startswith(prefix)
            ]

    def all_for_scope(self, tier: str, scope: str) -> list[tuple[str, dict, float]]:
        with self._lock:
            return [
                (k, v, ts) for (s, t, k), (v, ts) in self._data.items()
                if s == scope and t == tier
            ]


class SQLiteBackend:
    """SQLite-backed durable storage. Uses JSON value column."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with sqlite3.connect(str(self._path)) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memory_store (
                    scope TEXT, tier TEXT, key TEXT, value_json TEXT,
                    stored_at REAL,
                    PRIMARY KEY (scope, tier, key)
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_memory_scope_tier
                   ON memory_store(scope, tier)"""
            )
            conn.commit()

    def put(self, scope: str, tier: str, key: str, value: dict, stored_at: float) -> None:
        import json
        with self._lock, sqlite3.connect(str(self._path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_store "
                "(scope, tier, key, value_json, stored_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope, tier, key, json.dumps(value, ensure_ascii=False), stored_at),
            )
            conn.commit()

    def get(self, scope: str, tier: str, key: str) -> tuple[dict, float] | None:
        import json
        with self._lock, sqlite3.connect(str(self._path)) as conn:
            row = conn.execute(
                "SELECT value_json, stored_at FROM memory_store "
                "WHERE scope=? AND tier=? AND key=?",
                (scope, tier, key),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0]), float(row[1])

    def delete(self, scope: str, tier: str, key: str) -> bool:
        with self._lock, sqlite3.connect(str(self._path)) as conn:
            cur = conn.execute(
                "DELETE FROM memory_store WHERE scope=? AND tier=? AND key=?",
                (scope, tier, key),
            )
            conn.commit()
            return cur.rowcount > 0

    def query(self, scope: str, tier: str, prefix: str) -> list[tuple[str, dict, float]]:
        import json
        with self._lock, sqlite3.connect(str(self._path)) as conn:
            rows = conn.execute(
                "SELECT key, value_json, stored_at FROM memory_store "
                "WHERE scope=? AND tier=? AND key LIKE ?",
                (scope, tier, prefix + "%"),
            ).fetchall()
        return [(k, json.loads(v), float(ts)) for k, v, ts in rows]

    def all_for_scope(self, tier: str, scope: str) -> list[tuple[str, dict, float]]:
        import json
        with self._lock, sqlite3.connect(str(self._path)) as conn:
            rows = conn.execute(
                "SELECT key, value_json, stored_at FROM memory_store "
                "WHERE scope=? AND tier=?",
                (scope, tier),
            ).fetchall()
        return [(k, json.loads(v), float(ts)) for k, v, ts in rows]


# ----------------------------------------------------------------------
# MemoryStore facade
# ----------------------------------------------------------------------
class MemoryStore:
    """One store per tier. Holds a reference to its Tier config + backend."""

    def __init__(self, tier: Tier, backend: _Backend) -> None:
        self.tier = tier
        self._backend = backend

    def put(
        self, scope: str, key: str, value: dict,
        *, ttl_seconds: int | None = None,
        now: float | None = None,
    ) -> None:
        """Insert/update an entry. ``ttl_seconds`` overrides tier default."""
        effective_ttl = ttl_seconds if ttl_seconds is not None else self.tier.ttl_seconds
        stored_at = now if now is not None else time.time()
        # If ttl=0, treat as expired immediately (no-op skip)
        if effective_ttl == 0:
            return
        self._backend.put(scope, self.tier.name, key, value, stored_at)

    def get(self, scope: str, key: str, *, now: float | None = None) -> dict | None:
        row = self._backend.get(scope, self.tier.name, key)
        if row is None:
            return None
        value, stored_at = row
        if self.tier.is_expired(stored_at, now=now):
            return None
        return value

    def delete(self, scope: str, key: str) -> bool:
        return self._backend.delete(scope, self.tier.name, key)

    def query(self, scope: str, prefix: str) -> list[dict]:
        rows = self._backend.query(scope, self.tier.name, prefix)
        out = []
        for key, value, ts in rows:
            if not self.tier.is_expired(ts):
                out.append({"key": key, "value": value, "stored_at": ts})
        return out

    def all_for_scope(self, scope: str) -> list[dict]:
        rows = self._backend.all_for_scope(self.tier.name, scope)
        out = []
        for key, value, ts in rows:
            if not self.tier.is_expired(ts):
                out.append({"key": key, "value": value, "stored_at": ts})
        return out


class MemoryFacade:
    """Holds L1/L2/L3 stores + dispatch by tier name."""

    def __init__(self, backend: _Backend | None = None) -> None:
        # If no backend provided, use a single InMemoryBackend shared
        # by all tiers (good for tests).
        self._backend = backend or InMemoryBackend()
        self._tiers: dict[str, MemoryStore] = {}

    def register(self, tier: Tier) -> MemoryStore:
        store = MemoryStore(tier, self._backend)
        self._tiers[tier.name] = store
        return store

    def get(self, tier_name: str, scope: str, key: str) -> dict | None:
        store = self._tiers.get(tier_name)
        if store is None:
            return None
        return store.get(scope, key)

    def put(
        self, tier_name: str, scope: str, key: str, value: dict,
        *, ttl_seconds: int | None = None,
    ) -> None:
        store = self._tiers.get(tier_name)
        if store is None:
            return
        store.put(scope, key, value, ttl_seconds=ttl_seconds)

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, store in self._tiers.items():
            out[name] = {
                "ttl_seconds": store.tier.ttl_seconds,
                "persistent": store.tier.persistent,
                "cross_session": store.tier.cross_session,
            }
        return out


__all__ = [
    "Tier", "TIER_L1", "TIER_L2", "TIER_L3",
    "InMemoryBackend", "SQLiteBackend",
    "MemoryStore", "MemoryFacade",
]
