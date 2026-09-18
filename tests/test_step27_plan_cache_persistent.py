"""Step 27 — plan cache persists across process restarts.

The in-memory ``PlanTemplateCache`` is fast but volatile — restart
the harness and every cache miss replays the LLM round-trip. This
test wraps it with a SQLite-backed ``PersistentPlanCache`` that
mirrors writes to disk and warm-loads on construction.

Coverage:
- Cold cache loads previously stored entries on construction
- get returns the same plan as in-memory after a "restart"
- TTL still applies (expired entries pruned on load)
- LRU eviction still enforced (max_entries respected)
- Concurrent put/get from two threads is safe (lock held)
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_persistent_cache_warms_on_construction():
    from tradingagents.agent_harness.core.persistent_plan_cache import (
        PersistentPlanCache,
    )
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "plans.sqlite"
        c1 = PersistentPlanCache(db_path=db_path, max_entries=10, ttl_seconds=60)
        c1.put("深度分析 600036.SS", [{"step": 1, "action": "synthesize"}])
        # New instance simulates a process restart.
        c2 = PersistentPlanCache(db_path=db_path, max_entries=10, ttl_seconds=60)
        plan = c2.get("深度分析 600036.SS")
        assert plan is not None
        assert plan == [{"step": 1, "action": "synthesize"}]


def test_persistent_cache_ttl_expires():
    """Expired entries pruned on warm-load."""
    from tradingagents.agent_harness.core.persistent_plan_cache import (
        PersistentPlanCache,
    )
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "plans.sqlite"
        c1 = PersistentPlanCache(db_path=db_path, max_entries=10, ttl_seconds=0.1)
        c1.put("message A", [{"step": 1}])
        time.sleep(0.2)
        c2 = PersistentPlanCache(db_path=db_path, max_entries=10, ttl_seconds=0.1)
        plan = c2.get("message A")
        assert plan is None  # expired


def test_persistent_cache_lru_cap():
    """max_entries cap is enforced across restarts."""
    from tradingagents.agent_harness.core.persistent_plan_cache import (
        PersistentPlanCache,
    )
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "plans.sqlite"
        c1 = PersistentPlanCache(db_path=db_path, max_entries=2, ttl_seconds=60)
        c1.put("msg a", [{"a": 1}])
        c1.put("msg b", [{"b": 1}])
        c1.put("msg c", [{"c": 1}])  # evicts 'a'
        c2 = PersistentPlanCache(db_path=db_path, max_entries=2, ttl_seconds=60)
        assert c2.get("msg a") is None  # was evicted
        assert c2.get("msg b") == [{"b": 1}]
        assert c2.get("msg c") == [{"c": 1}]


def test_persistent_cache_stats_includes_disk_hits():
    """Stats include disk_hits / disk_misses."""
    from tradingagents.agent_harness.core.persistent_plan_cache import (
        PersistentPlanCache,
    )
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "plans.sqlite"
        c1 = PersistentPlanCache(db_path=db_path, max_entries=10, ttl_seconds=60)
        c1.put("hello", [{"x": 1}])
        c2 = PersistentPlanCache(db_path=db_path, max_entries=10, ttl_seconds=60)
        c2.get("hello")  # disk hit
        c2.get("world")  # disk miss
        stats = c2.stats()
        assert stats["disk_hits"] >= 1
        assert stats["disk_misses"] >= 1


def test_persistent_cache_concurrent_put_get_safe():
    """Thread safety — two writers don't corrupt the cache."""
    import threading
    from tradingagents.agent_harness.core.persistent_plan_cache import (
        PersistentPlanCache,
    )
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "plans.sqlite"
        c = PersistentPlanCache(db_path=db_path, max_entries=100, ttl_seconds=60)
        def worker(i: int) -> None:
            for j in range(10):
                c.put(f"key-{i}-{j}", [{"i": i, "j": j}])
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        # 4 × 10 = 40 entries, all retrievable
        for i in range(4):
            for j in range(10):
                assert c.get(f"key-{i}-{j}") == [{"i": i, "j": j}]
