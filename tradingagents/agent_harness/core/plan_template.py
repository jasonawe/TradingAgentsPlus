"""Plan template cache (v3 spec §7.3 #6, N65 fix).

Caches ``user_message -> plan`` mappings so repeated similar queries can
skip the LLM plan step.  Reuse conditions (per spec):

- user_message 完全相同(忽略空格/标点)
- 模板在 cache 中存在
- 复用时间窗 ≤ 5 分钟

否则重新生成 plan。

Thread-safe LRU + TTL cache.  Sized small (256 entries) to bound memory.
Stats counters (``hits/misses/evictions/expirations``) make cache
behaviour observable in tests + debug.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300.0      # 5 minutes (N65 fix)
DEFAULT_MAX_ENTRIES = 256

# Strip punctuation (unicode-safe) and collapse whitespace.
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_message(message: str) -> str:
    """归一化 message — 忽略空格/标点,小写。

    >>> normalize_message("  Hello,  World!  ")
    'hello world'
    >>> normalize_message("分析 600036 估值")
    '分析 600036 估值'
    """
    s = (message or "").strip().lower()
    s = _PUNCT_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


@dataclass
class _CacheEntry:
    plan: Any
    stored_at: float


class PlanTemplateCache:
    """TTL + LRU plan cache.

    Parameters
    ----------
    ttl_seconds
        Cache entry lifetime.  N65 fix says 5 minutes.
    max_entries
        Hard cap on cache size; oldest entries get evicted (LRU).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        if max_entries <= 0:
            raise ValueError("max_entries must be > 0")
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, _CacheEntry]" = OrderedDict()
        # Stats — bumped under lock.
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------
    def get(self, message: str) -> Any | None:
        """Return the cached plan for ``message`` if still fresh, else None.

        LRU-touch on hit.  Counts miss for missing / expired / empty key.
        """
        key = normalize_message(message)
        if not key:
            self.misses += 1
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if now - entry.stored_at > self.ttl_seconds:
                # Expired — drop on read.
                self._entries.pop(key, None)
                self.expirations += 1
                self.misses += 1
                return None
            # Touch for LRU.
            self._entries.move_to_end(key)
            self.hits += 1
            return entry.plan

    def put(self, message: str, plan: Any) -> None:
        """Store ``plan`` under the normalised key for ``message``.

        No-op for empty / falsy keys (e.g. blank query) or ``plan is None``.
        Overwrites an existing entry (refreshes timestamp) and updates
        LRU position.
        """
        key = normalize_message(message)
        if not key or plan is None:
            return
        now = time.monotonic()
        with self._lock:
            self._evict_expired_locked(now)
            self._entries[key] = _CacheEntry(plan=plan, stored_at=now)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1

    def invalidate(self, message: str) -> bool:
        """Drop the entry for ``message`` (returns True if a key was removed)."""
        key = normalize_message(message)
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "expirations": self.expirations,
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _evict_expired_locked(self, now: float) -> None:
        """Drop expired entries.  Caller must hold ``self._lock``.

        Linear scan — entries are not guaranteed to be in monotonic
        ``stored_at`` order because ``move_to_end`` reorders them on
        LRU touch.  With ``max_entries`` ≤ 256 the scan is cheap.
        """
        expired_keys: list[str] = [
            k for k, e in self._entries.items()
            if now - e.stored_at > self.ttl_seconds
        ]
        for k in expired_keys:
            self._entries.pop(k, None)
            self.expirations += 1
