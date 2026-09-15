"""LLM response cache — short-circuits identical LLM calls.

Why this exists
---------------
LLM calls are slow + expensive. Many harness requests issue the same
prompt twice:

- Repeated user queries for the same symbol ("查 600036" twice)
- Heuristic plan replanning on L3 verification failure
- Plan calls at temperature=0 are fully deterministic

A small cache (in-memory + optional SQLite) eliminates these redundant
calls. The cache is **opt-in** — providers without a cache configured
behave exactly as before.

Wire-in
-------
``OpenAICompatibleProvider.complete()`` consults the cache before the
real LLM call. On hit it returns the cached response + records an
``usage`` dict with ``cached_tokens`` so token accounting can attribute
the saved cost. On miss it stores the response for next time.

Cache key
---------
SHA-256 over ``(messages + system + temperature + max_tokens + model)``.
System + user messages are concatenated with role separators to avoid
ordering collisions.

TTL
---
Default 1 hour — long enough for replanning loops, short enough that
quote/fundamental data stays fresh. Configurable via ``ttl_seconds``.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

from .base import ChatMessage, LLMResponse

LOGGER = logging.getLogger(__name__)


def _stable_hash(*parts: str) -> str:
    """SHA-256 hex digest of concatenated parts (deterministic ordering)."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\\x00")
    return h.hexdigest()


def make_cache_key(
    messages: Iterable[ChatMessage],
    *,
    system: str | None,
    temperature: float,
    max_tokens: int | None,
    model: str,
) -> str:
    """Build a stable cache key from the request envelope.

    Order of fields matters for collision-resistance; only add fields
    when you also add them to the key, or you'll silently get wrong
    hits.
    """
    msg_str = "|".join(f"{m.role}:{m.content}" for m in messages)
    return _stable_hash(
        model,
        system or "",
        msg_str,
        f"temp={temperature}",
        f"max={max_tokens or 0}",
    )


@dataclass
class CacheEntry:
    """Single cached response."""

    response: LLMResponse
    created_at: float  # time.time()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at


class LLMResponseCache:
    """In-memory + optional SQLite-backed LLM response cache.

    Usage::

        cache = LLMResponseCache(ttl_seconds=3600)
        provider = OpenAICompatibleProvider(..., cache=cache)
        # ... calls go through the cache automatically ...

    Statistics (``hits``, ``misses``, ``stores``) are exposed via
    :attr:`stats` for the metrics endpoint to report hit rate.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        max_entries: int = 1024,
        sqlite_store: Any | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._sqlite = sqlite_store
        # in-memory: key -> CacheEntry
        self._mem: dict[str, CacheEntry] = {}
        # LRU tracking — move-to-end on hit; evict from front
        self._lru_keys: list[str] = []
        # stats
        self.hits: int = 0
        self.misses: int = 0
        self.stores: int = 0
        self.evictions: int = 0

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------
    def get(self, key: str) -> LLMResponse | None:
        """Return cached response if present + fresh, else None.

        Updates LRU order on hit.
        """
        entry = self._mem.get(key)
        if entry is None:
            if self._sqlite is not None:
                entry = self._sqlite_lookup(key)
            if entry is None:
                self.misses += 1
                return None
        if entry.age_seconds > self._ttl:
            # Expired — drop and miss
            self._mem.pop(key, None)
            if key in self._lru_keys:
                self._lru_keys.remove(key)
            self.misses += 1
            return None
        # Hit — refresh LRU
        self.hits += 1
        if key in self._lru_keys:
            self._lru_keys.remove(key)
        self._lru_keys.append(key)
        return entry.response

    def put(self, key: str, response: LLMResponse) -> None:
        """Store a response under ``key``.

        Evicts oldest entry if over capacity.
        """
        entry = CacheEntry(response=response, created_at=time.time())
        # LRU eviction
        if key not in self._mem and len(self._mem) >= self._max:
            oldest = self._lru_keys.pop(0)
            self._mem.pop(oldest, None)
            self.evictions += 1
        self._mem[key] = entry
        if key in self._lru_keys:
            self._lru_keys.remove(key)
        self._lru_keys.append(key)
        self.stores += 1
        if self._sqlite is not None:
            self._sqlite_store(key, entry)

    def clear(self) -> None:
        """Drop all in-memory entries (tests + manual cache busting)."""
        self._mem.clear()
        self._lru_keys.clear()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        hit_rate = self.hits / total if total else 0.0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "evictions": self.evictions,
            "size": len(self._mem),
            "max": self._max,
            "ttl_seconds": self._ttl,
            "hit_rate": hit_rate,
        }

    # ------------------------------------------------------------------
    # SQLite persistence (optional)
    # ------------------------------------------------------------------
    _SQLITE_TABLE = "llm_response_cache"

    def _sqlite_lookup(self, key: str) -> CacheEntry | None:
        if self._sqlite is None:
            return None
        try:
            with self._sqlite._connect() as conn:
                row = conn.execute(
                    f"SELECT response_json, created_at FROM {self._SQLITE_TABLE} WHERE key = ?",
                    (key,),
                ).fetchone()
            if row is None:
                return None
            # Hydrate LLMResponse from JSON
            import json
            payload = json.loads(row["response_json"])
            return CacheEntry(
                response=LLMResponse(**payload),
                created_at=float(row["created_at"]),
            )
        except Exception:
            LOGGER.debug("cache sqlite lookup failed", exc_info=True)
            return None

    def _sqlite_store(self, key: str, entry: CacheEntry) -> None:
        try:
            import json
            payload = entry.response.model_dump()
            with self._sqlite._connect() as conn:
                conn.execute(
                    f"INSERT OR REPLACE INTO {self._SQLITE_TABLE} (key, response_json, created_at) "
                    "VALUES (?, ?, ?)",
                    (key, json.dumps(payload, default=str), entry.created_at),
                )
                conn.commit()
        except Exception:
            LOGGER.debug("cache sqlite store failed", exc_info=True)
