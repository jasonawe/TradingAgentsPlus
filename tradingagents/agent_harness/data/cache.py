"""§7.3 #4 — Tool result cache.

Per-process in-memory cache for tool invocations.  When a tool's
``ToolSchema.cache_ttl_seconds > 0``, :class:`FunctionTool` wraps the
inner call with :meth:`ToolResultCache.get_or_set`.  The cache key is
``(tool_name, stable_hash_of_args)`` so:

- Different tools with the same args don't collide.
- Same tool with the same args is a hit within the TTL window.
- Different args (model order, value) hash differently (we sort the
  model dump keys before hashing).

Per spec §9 P2 (N62 fix): default TTL 60s, key = provider+endpoint+params.
We extend that with a process-local LRU bound (default 1024 entries) so
long-running sessions don't leak.

For multi-process / multi-host correctness the spec mentioned SQLite,
but the in-process cache covers 99% of "make repeated UI polls free"
and avoids any disk I/O on the read path.  A SQLite layer can slot in
behind this interface without changing the call sites.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class _Entry:
    value: Any
    expires_at: float


class ToolResultCache:
    """In-memory tool result cache with TTL + LRU eviction.

    Thread-safe is **not** required: the harness invokes tools from a
    single async event loop, so there's no concurrent mutation.
    """

    def __init__(self, *, max_entries: int = 1024, time_source: Callable[[], float] = time.monotonic) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._max = int(max_entries)
        self._now = time_source

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------
    @staticmethod
    def make_key(tool_name: str, args: Any) -> str:
        """Derive a stable cache key from tool name + Pydantic/dict args.

        Falls back to ``repr(args)`` if neither ``model_dump_json`` nor a
        mapping is available — callers should not rely on this path for
        stateful / non-hashable args, but it keeps the cache usable for
        any input shape (test ergonomics).
        """
        if args is None:
            payload = "{}"
        elif hasattr(args, "model_dump_json"):
            # Pydantic v2 — emit stable JSON via sort_keys.
            raw = args.model_dump(mode="json")
            payload = json.dumps(raw, sort_keys=True, default=str)
        elif isinstance(args, dict):
            payload = json.dumps(args, sort_keys=True, default=str)
        else:
            payload = repr(args)
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        return f"{tool_name}:{digest}"

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------
    def get(self, key: str) -> Optional[Any]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at < self._now():
            # Expired — evict on read.
            self._entries.pop(key, None)
            return None
        # LRU touch.
        self._entries.move_to_end(key)
        return entry.value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return  # TTL 0 = disabled
        self._entries[key] = _Entry(
            value=value,
            expires_at=self._now() + ttl_seconds,
        )
        self._entries.move_to_end(key)
        # Evict oldest if over capacity.
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def invalidate(self, key: Optional[str] = None) -> int:
        if key is None:
            n = len(self._entries)
            self._entries.clear()
            return n
        return 1 if self._entries.pop(key, None) is not None else 0

    def stats(self) -> dict[str, int]:
        return {"size": len(self._entries), "max": self._max}

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Module-level default cache (singletons per process)
# ---------------------------------------------------------------------------
_DEFAULT: Optional[ToolResultCache] = None


def get_default_cache() -> ToolResultCache:
    """Return the process-wide default cache, instantiating lazily."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = ToolResultCache()
    return _DEFAULT


def reset_default_cache() -> None:
    """Drop the default cache — used by tests and on hot-reload."""
    global _DEFAULT
    _DEFAULT = None
