"""Step 45 — memory tier merging (D4)."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ------------------------------------------------------------------
# Tier config
# ------------------------------------------------------------------
def test_tier_is_expired_with_ttl():
    from tradingagents.agent_harness.memory.store import Tier
    t = Tier(name="t", ttl_seconds=10)
    now = 1000.0
    assert not t.is_expired(now - 5, now=now)
    assert t.is_expired(now - 11, now=now)


def test_tier_with_no_ttl_never_expires():
    from tradingagents.agent_harness.memory.store import Tier
    t = Tier(name="t", ttl_seconds=None)
    assert not t.is_expired(0)
    assert not t.is_expired(time.time() - 10**9)


# ------------------------------------------------------------------
# MemoryStore: put/get/query/delete with InMemoryBackend
# ------------------------------------------------------------------
def test_inmemory_backend_basic_crud():
    from tradingagents.agent_harness.memory.store import (
        InMemoryBackend, MemoryStore, Tier,
    )
    be = InMemoryBackend()
    tier = Tier(name="l1", ttl_seconds=60)
    store = MemoryStore(tier, be)
    store.put("s1", "k1", {"v": 1})
    assert store.get("s1", "k1") == {"v": 1}
    store.delete("s1", "k1")
    assert store.get("s1", "k1") is None


def test_tier_ttl_expires_entries():
    from tradingagents.agent_harness.memory.store import (
        InMemoryBackend, MemoryStore, Tier,
    )
    be = InMemoryBackend()
    tier = Tier(name="l1", ttl_seconds=10)
    store = MemoryStore(tier, be)
    store.put("s", "k", {"v": 1}, now=1000.0)
    assert store.get("s", "k", now=1000.0) == {"v": 1}
    # 11 seconds later, expired.
    assert store.get("s", "k", now=1011.0) is None


def test_per_tier_put_get_overrides_dont_collide():
    """L1 and L2 with same key + scope are isolated."""
    from tradingagents.agent_harness.memory.store import (
        InMemoryBackend, MemoryStore, Tier,
    )
    be = InMemoryBackend()
    l1 = MemoryStore(Tier(name="l1", ttl_seconds=60), be)
    l2 = MemoryStore(Tier(name="l2", ttl_seconds=86400), be)
    l1.put("s", "k", {"from": "l1"})
    l2.put("s", "k", {"from": "l2"})
    assert l1.get("s", "k") == {"from": "l1"}
    assert l2.get("s", "k") == {"from": "l2"}


def test_query_returns_only_matching_prefix():
    from tradingagents.agent_harness.memory.store import (
        InMemoryBackend, MemoryStore, Tier,
    )
    be = InMemoryBackend()
    store = MemoryStore(Tier(name="l1", ttl_seconds=60), be)
    store.put("s", "note:1", {"v": 1})
    store.put("s", "note:2", {"v": 2})
    store.put("s", "alert:1", {"v": 3})
    notes = store.query("s", "note:")
    assert len(notes) == 2
    assert {n["key"] for n in notes} == {"note:1", "note:2"}


def test_memory_facade_routes_by_tier_name():
    from tradingagents.agent_harness.memory.store import (
        InMemoryBackend, MemoryFacade, TIER_L1, TIER_L2, TIER_L3,
    )
    f = MemoryFacade(InMemoryBackend())
    f.register(TIER_L1)
    f.register(TIER_L2)
    f.register(TIER_L3)
    f.put("l1", "s", "k", {"a": 1})
    f.put("l2", "s", "k", {"a": 2})
    f.put("l3", "s", "k", {"a": 3})
    assert f.get("l1", "s", "k") == {"a": 1}
    assert f.get("l2", "s", "k") == {"a": 2}
    assert f.get("l3", "s", "k") == {"a": 3}
    stats = f.stats()
    assert "l1" in stats and "l2" in stats and "l3" in stats


# ------------------------------------------------------------------
# SQLite backend durability
# ------------------------------------------------------------------
def test_sqlite_backend_round_trip():
    from tradingagents.agent_harness.memory.store import (
        MemoryStore, SQLiteBackend, Tier,
    )
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "mem.sqlite3"
    backend = SQLiteBackend(db)
    store = MemoryStore(Tier(name="l2", ttl_seconds=None), backend)
    store.put("s1", "k1", {"v": "hello"})
    assert store.get("s1", "k1") == {"v": "hello"}
    # Open a second store pointing at the same DB (simulates process restart).
    backend2 = SQLiteBackend(db)
    store2 = MemoryStore(Tier(name="l2", ttl_seconds=None), backend2)
    assert store2.get("s1", "k1") == {"v": "hello"}


def test_sqlite_backend_delete_removes_row():
    from tradingagents.agent_harness.memory.store import (
        MemoryStore, SQLiteBackend, Tier,
    )
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "mem.sqlite3"
    backend = SQLiteBackend(db)
    store = MemoryStore(Tier(name="l2", ttl_seconds=None), backend)
    store.put("s", "k", {"v": 1})
    assert store.delete("s", "k") is True
    assert store.delete("s", "k") is False  # already gone
    assert store.get("s", "k") is None


def test_sqlite_backend_query_with_prefix():
    from tradingagents.agent_harness.memory.store import (
        MemoryStore, SQLiteBackend, Tier,
    )
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "mem.sqlite3"
    backend = SQLiteBackend(db)
    store = MemoryStore(Tier(name="l3", ttl_seconds=None), backend)
    store.put("s", "ref:1", {"v": "a"})
    store.put("s", "ref:2", {"v": "b"})
    store.put("s", "other:1", {"v": "c"})
    rows = store.query("s", "ref:")
    assert {r["key"] for r in rows} == {"ref:1", "ref:2"}


def test_backend_interchangeable():
    """Same Tier + same API works with InMemory or SQLite backend."""
    from tradingagents.agent_harness.memory.store import (
        InMemoryBackend, MemoryStore, SQLiteBackend, Tier,
    )
    tier = Tier(name="l2", ttl_seconds=60)
    for backend in [InMemoryBackend(), SQLiteBackend(Path(tempfile.mkdtemp()) / "x.db")]:
        s = MemoryStore(tier, backend)
        s.put("scope", "key", {"v": 1})
        assert s.get("scope", "key") == {"v": 1}
