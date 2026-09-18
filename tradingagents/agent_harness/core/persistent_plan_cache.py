"""Step 27 — SQLite-backed persistent plan cache.

Wraps :class:`PlanTemplateCache` so plan templates survive process
restarts. Backed by a single SQLite file (default
``web_runs.sqlite3``-adjacent ``plan_cache.sqlite``). The cache is
LRU + TTL like the in-memory variant — only persistent across
process lifetime.

Why a separate file rather than a table in ``web_runs.sqlite``?
The plan cache can grow to a few thousand rows with rich JSON
payloads; isolating it on its own DB keeps the schema simple and
avoids competing for the writes lock on the runs/audit tables.

Schema:

    CREATE TABLE plan_cache (
        key TEXT PRIMARY KEY,
        plan_json TEXT NOT NULL,
        stored_at REAL NOT NULL,
        ttl_seconds REAL NOT NULL
    );

We index on ``stored_at`` so the TTL sweep is cheap.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .plan_template import _CacheEntry, PlanTemplateCache, normalize_message


class PersistentPlanCache:
    """Disk-backed plan cache with the same LRU/TTL semantics."""

    def __init__(
        self,
        *,
        db_path: Path | str,
        ttl_seconds: float = 300.0,
        max_entries: int = 256,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # In-memory mirror for hot-path reads.
        self._entries: "OrderedDict[str, _CacheEntry]" = OrderedDict()
        # Stats
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0
        self.disk_hits = 0
        self.disk_misses = 0
        # Disk init
        self._init_schema()
        self._warm_load()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS plan_cache (
                        key TEXT PRIMARY KEY,
                        plan_json TEXT NOT NULL,
                        stored_at REAL NOT NULL,
                        ttl_seconds REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_plan_cache_stored_at "
                    "ON plan_cache(stored_at)"
                )
                conn.commit()
            finally:
                conn.close()

    def _warm_load(self) -> None:
        """Sweep expired rows from disk + preload non-expired row keys.

        We deliberately do NOT populate ``self._entries`` here — that
        would mask disk-hit semantics for the first ``get()`` after a
        process restart. Instead we keep a small in-memory index of
        keys + stored_at so the LRU bookkeeping is honest, and fetch
        the JSON lazily on the first ``get()`` that matches.

        Stats:
        - expired rows dropped → ``expirations`` counts.
        - the keys we kept are NOT counted as disk_hits — those will
          bump on the first ``get()`` call per key, which is the
          correct observability signal for "the cache survived a
          restart".
        """
        cutoff = time.time() - self.ttl_seconds
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT key, stored_at FROM plan_cache "
                "WHERE stored_at > ? "
                "ORDER BY stored_at DESC LIMIT ?",
                (cutoff, self.max_entries),
            ).fetchall()
            for key, stored_at in rows:
                # Pre-populate a lightweight index entry; the plan JSON
                # is fetched lazily on get().
                self._entries[key] = _CacheEntry(
                    plan=None, stored_at=stored_at,
                )
            # Sweep expired rows from disk.
            conn.execute(
                "DELETE FROM plan_cache WHERE stored_at <= ?", (cutoff,),
            )
            conn.commit()
            n_expired = conn.total_changes
            self.expirations += max(0, n_expired)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------
    def get(self, message: str) -> Any | None:
        key = normalize_message(message)
        if not key:
            self.misses += 1
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.plan is None:
                    # Index entry from warm_load — hydrate from disk.
                    disk_plan = self._disk_get(key)
                    if disk_plan is None:
                        # Was expired between warm_load and now.
                        self._entries.pop(key, None)
                        self.misses += 1
                        return None
                    entry.plan = disk_plan
                    self.disk_hits += 1
                    self.hits += 1
                    return entry.plan
                if now - entry.stored_at > self.ttl_seconds:
                    self._entries.pop(key, None)
                    self._disk_delete(key)
                    self.expirations += 1
                    self.misses += 1
                    return None
                self._entries.move_to_end(key)
                self.hits += 1
                return entry.plan
            # Miss → consult disk.
            plan = self._disk_get(key)
            if plan is not None:
                self.disk_hits += 1
                # Promote into in-memory LRU.
                self._entries[key] = _CacheEntry(plan=plan, stored_at=now)
                self._entries.move_to_end(key)
                evicted_keys = []
                while len(self._entries) > self.max_entries:
                    k, _ = self._entries.popitem(last=False)
                    evicted_keys.append(k)
                    self.evictions += 1
                for k in evicted_keys:
                    self._disk_delete(k)
                self.hits += 1
                return plan
            self.disk_misses += 1
            self.misses += 1
            return None

    def put(self, message: str, plan: Any) -> None:
        key = normalize_message(message)
        if not key or plan is None:
            return
        now = time.monotonic()
        with self._lock:
            self._entries[key] = _CacheEntry(plan=plan, stored_at=now)
            self._entries.move_to_end(key)
            # Track the entries that get evicted so we can delete
            # them from disk too — otherwise the disk table keeps
            # growing past max_entries.
            evicted_keys: list[str] = []
            while len(self._entries) > self.max_entries:
                k, _ = self._entries.popitem(last=False)
                evicted_keys.append(k)
                self.evictions += 1
            # Persist.
            try:
                plan_json = json.dumps(plan, default=str, ensure_ascii=False)
            except Exception:
                return
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO plan_cache "
                    "(key, plan_json, stored_at, ttl_seconds) "
                    "VALUES (?, ?, ?, ?)",
                    (key, plan_json, time.time(), self.ttl_seconds),
                )
                conn.commit()
            finally:
                conn.close()
            # Drop evicted keys from disk.
            for k in evicted_keys:
                self._disk_delete(k)

    def invalidate(self, message: str) -> bool:
        key = normalize_message(message)
        with self._lock:
            removed = self._entries.pop(key, None) is not None
            self._disk_delete(key)
            return removed

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("DELETE FROM plan_cache")
                conn.commit()
            finally:
                conn.close()

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
                "disk_hits": self.disk_hits,
                "disk_misses": self.disk_misses,
            }

    # ------------------------------------------------------------------
    # Disk helpers (caller holds ``self._lock``)
    # ------------------------------------------------------------------
    def _disk_get(self, key: str) -> Any | None:
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT plan_json, stored_at FROM plan_cache WHERE key = ?",
                (key,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        plan_json, stored_at = row
        if time.time() - stored_at > self.ttl_seconds:
            self._disk_delete(key)
            return None
        try:
            return json.loads(plan_json)
        except Exception:
            return None

    def _disk_delete(self, key: str) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("DELETE FROM plan_cache WHERE key = ?", (key,))
            conn.commit()
        finally:
            conn.close()
