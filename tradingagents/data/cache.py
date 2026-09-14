"""SQLite-backed provider cache (N62 fix).

Keys are derived from ``(provider, endpoint, params)`` so the same
endpoint on a different provider does NOT collide. TTL defaults to 60
seconds (configurable per call); expired entries are filtered on read.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_TTL_SECONDS = 60
_CACHE_DIR = Path(os.environ.get("TRADINGAGENTS_CACHE_DIR", ".ta_cache"))


def make_key(provider: str, endpoint: str, params: dict[str, Any]) -> str:
    """Stable JSON-sorted cache key.

    Examples
    --------
    >>> make_key("yfinance", "quote", {"symbol": "AAPL"})
    'yfinance::quote::{"symbol":"AAPL"}'
    """
    return f"{provider}::{endpoint}::{json.dumps(params, sort_keys=True, default=str)}"


class ProviderCache:
    """Tiny TTL cache used to dedupe provider calls inside a 60 s window."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        default_ttl: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._db_path = Path(db_path) if db_path else _CACHE_DIR / "provider_cache.sqlite"
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
                CREATE TABLE IF NOT EXISTS provider_cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get(self, key: str) -> Optional[str]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value, expires_at FROM provider_cache WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        if float(row["expires_at"]) < time.time():
            self.delete(key)
            return None
        return row["value"]

    def set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        ttl = ttl if ttl is not None else self.default_ttl
        expires_at = time.time() + ttl
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO provider_cache(key, value, expires_at) VALUES (?, ?, ?)",
                (key, value, expires_at),
            )
            conn.commit()

    def delete(self, key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM provider_cache WHERE key = ?", (key,))
            conn.commit()

    def clear(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM provider_cache")
            conn.commit()


_default_cache: Optional[ProviderCache] = None
_default_cache_lock = threading.Lock()


def get_default_cache() -> ProviderCache:
    """Process-wide singleton used by :func:`cached_call`."""
    global _default_cache
    with _default_cache_lock:
        if _default_cache is None:
            _default_cache = ProviderCache()
        return _default_cache


def cached_call(
    provider: str,
    endpoint: str,
    params: dict[str, Any],
    compute,
    *,
    ttl: Optional[int] = None,
    cache: Optional[ProviderCache] = None,
):
    """Return cached JSON ``str`` if present, otherwise call ``compute``."""
    cache = cache or get_default_cache()
    key = make_key(provider, endpoint, params)
    hit = cache.get(key)
    if hit is not None:
        return hit, True
    value = compute()
    cache.set(key, value, ttl=ttl)
    return value, False
