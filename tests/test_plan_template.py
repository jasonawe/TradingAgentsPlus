"""Tests for PlanTemplateCache (v3 spec §7.3 #6, N65 fix).

覆盖:
- normalize_message: 空格/标点/大小写/Unicode
- get/put: hit, miss, empty key, None plan
- TTL expiry: get 后过期返 None 并 bump expirations
- LRU eviction: 超 max_entries 驱逐最早的
- invalidate / clear
- stats counters
- thread-safe (concurrent put/get)
"""
from __future__ import annotations

import threading
import time

import pytest


# ---------------------------------------------------------------------------
# normalize_message
# ---------------------------------------------------------------------------
def test_normalize_strips_whitespace_and_punctuation():
    from tradingagents.agent_harness.core.plan_template import normalize_message
    assert normalize_message("  Hello,  World!  ") == "hello world"


def test_normalize_lowercases():
    from tradingagents.agent_harness.core.plan_template import normalize_message
    assert normalize_message("ANALYZE 600036") == "analyze 600036"


def test_normalize_handles_chinese_punctuation():
    from tradingagents.agent_harness.core.plan_template import normalize_message
    # 标点都是非 \w, 全部被剥掉
    assert normalize_message("分析, 600036 估值。") == "分析 600036 估值"


def test_normalize_collapses_internal_whitespace():
    from tradingagents.agent_harness.core.plan_template import normalize_message
    assert normalize_message("a   b\n\nc\td") == "a b c d"


def test_normalize_handles_empty_and_none():
    from tradingagents.agent_harness.core.plan_template import normalize_message
    assert normalize_message("") == ""
    assert normalize_message("   ") == ""
    assert normalize_message(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def test_construction_rejects_invalid_args():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    with pytest.raises(ValueError):
        PlanTemplateCache(ttl_seconds=0)
    with pytest.raises(ValueError):
        PlanTemplateCache(ttl_seconds=-1)
    with pytest.raises(ValueError):
        PlanTemplateCache(max_entries=0)


def test_default_construction_has_expected_cap():
    from tradingagents.agent_harness.core.plan_template import (
        PlanTemplateCache,
        DEFAULT_MAX_ENTRIES,
        DEFAULT_TTL_SECONDS,
    )
    c = PlanTemplateCache()
    assert c.max_entries == DEFAULT_MAX_ENTRIES
    assert c.ttl_seconds == DEFAULT_TTL_SECONDS
    assert c.size == 0
    assert c.stats() == {"size": 0, "hits": 0, "misses": 0, "evictions": 0, "expirations": 0}


# ---------------------------------------------------------------------------
# get / put
# ---------------------------------------------------------------------------
def test_get_returns_none_on_miss():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache()
    assert c.get("analyze 600036") is None
    assert c.stats()["misses"] == 1
    assert c.stats()["hits"] == 0


def test_put_then_get_returns_same_plan():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache()
    plan = [{"step": 1, "action": "get_quote", "args": {"symbol": "600036.SS"}}]
    c.put("分析 600036 估值", plan)
    assert c.get("分析 600036 估值") == plan
    assert c.stats()["hits"] == 1
    assert c.stats()["misses"] == 0


def test_normalized_key_collapses_equivalent_messages():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache()
    plan = [{"step": 1, "action": "get_quote", "args": {"symbol": "AAPL"}}]
    c.put("Get Quote for AAPL!", plan)
    # 忽略空格/标点 + 小写后,这两个 message 是同一个 key
    assert c.get("get quote for aapl") == plan
    assert c.get("GET,  QUOTE;  FOR AAPL?") == plan


def test_put_empty_message_is_noop():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache()
    c.put("", [{"step": 1}])
    c.put("   ", [{"step": 1}])
    assert c.size == 0


def test_put_none_plan_is_noop():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache()
    c.put("analyze", None)
    assert c.size == 0


def test_put_overwrites_existing_entry():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache()
    c.put("query", ["plan_v1"])
    c.put("query", ["plan_v2"])
    assert c.size == 1
    assert c.get("query") == ["plan_v2"]


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------
def test_ttl_expiry_drops_entry_on_get():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache(ttl_seconds=0.1)
    c.put("analyze", ["plan"])
    assert c.get("analyze") == ["plan"]
    # Wait past TTL
    time.sleep(0.15)
    assert c.get("analyze") is None
    assert c.stats()["expirations"] == 1
    assert c.size == 0


def test_put_evicts_expired_entries():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache(ttl_seconds=0.1, max_entries=8)
    c.put("old_a", ["p1"])
    c.put("old_b", ["p2"])
    assert c.size == 2
    time.sleep(0.15)
    # New put triggers eviction of expired entries
    c.put("new", ["p3"])
    assert c.size == 1
    assert c.get("new") == ["p3"]
    assert c.stats()["expirations"] >= 2


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------
def test_lru_evicts_oldest_when_over_capacity():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache(ttl_seconds=300, max_entries=3)
    c.put("a", ["p_a"])
    c.put("b", ["p_b"])
    c.put("c", ["p_c"])
    assert c.size == 3
    # 4th insert evicts "a" (oldest by insertion order)
    c.put("d", ["p_d"])
    assert c.size == 3
    assert c.get("a") is None
    assert c.get("b") == ["p_b"]
    assert c.get("c") == ["p_c"]
    assert c.get("d") == ["p_d"]
    assert c.stats()["evictions"] == 1


def test_lru_touch_on_get_promotes_entry():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache(ttl_seconds=300, max_entries=3)
    c.put("a", ["p_a"])
    c.put("b", ["p_b"])
    c.put("c", ["p_c"])
    # Touch "a" → 它被 move_to_end,不再是 LRU victim
    assert c.get("a") == ["p_a"]
    # Insert "d" → 应该驱逐 "b" 而不是 "a"
    c.put("d", ["p_d"])
    assert c.get("a") == ["p_a"]
    assert c.get("b") is None
    assert c.get("c") == ["p_c"]
    assert c.get("d") == ["p_d"]


# ---------------------------------------------------------------------------
# invalidate / clear
# ---------------------------------------------------------------------------
def test_invalidate_drops_specific_entry():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache()
    c.put("a", ["pa"])
    c.put("b", ["pb"])
    assert c.invalidate("A!") is True   # 归一化后命中 "a"
    assert c.invalidate("A!") is False  # 第二次返 False
    assert c.size == 1
    assert c.get("b") == ["pb"]


def test_clear_drops_everything():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache()
    c.put("a", ["pa"])
    c.put("b", ["pb"])
    c.clear()
    assert c.size == 0
    assert c.get("a") is None
    assert c.get("b") is None


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------
def test_concurrent_put_get_is_safe():
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache(ttl_seconds=10.0, max_entries=64)
    errors: list[str] = []

    def writer(prefix: str, n: int):
        try:
            for i in range(n):
                c.put(f"{prefix}_{i}", [prefix, i])
        except Exception as e:  # pragma: no cover - smoke test
            errors.append(f"writer {prefix}: {e!r}")

    def reader(prefix: str, n: int):
        try:
            for i in range(n):
                c.get(f"{prefix}_{i}")
        except Exception as e:  # pragma: no cover - smoke test
            errors.append(f"reader {prefix}: {e!r}")

    threads = []
    for t in range(4):
        threads.append(threading.Thread(target=writer, args=(f"w{t}", 50)))
        threads.append(threading.Thread(target=reader, args=(f"w{t}", 50)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # 4 writers × 50 keys = 200 distinct keys, capped at 64
    assert c.size == 64
    stats = c.stats()
    assert stats["hits"] + stats["misses"] == 4 * 50
    assert stats["evictions"] >= 200 - 64


# ---------------------------------------------------------------------------
# Plan-shape acceptance (smoke)
# ---------------------------------------------------------------------------
def test_cache_handles_pydantic_plan_shape():
    """The cache stores opaque plans.  We exercise the common shapes
    (list of step dicts + PTC program) end-to-end."""
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    c = PlanTemplateCache()
    list_plan = [
        {"step": 1, "action": "get_quote", "args": {"symbol": "600036.SS"}},
    ]
    ptc_plan = {
        "mode": "ptc",
        "groups": [{
            "id": "g1",
            "calls": [
                {"name": "get_quote", "args": {"symbol": "600036.SS"}},
                {"name": "get_quote", "args": {"symbol": "600000.SS"}},
            ],
        }],
    }
    c.put("list query", list_plan)
    c.put("ptc query", ptc_plan)
    assert c.get("list  query") == list_plan
    assert c.get("ptc  query") == ptc_plan


# ---------------------------------------------------------------------------
# _plan integration (orchestrator wire-up) — sync wrappers via asyncio.run
# ---------------------------------------------------------------------------
def test_plan_node_uses_cache_on_second_call():
    """Second identical _plan invocation should hit the cache and skip
    the heuristic / LLM path."""
    import asyncio
    import pathlib
    from tradingagents.agent_harness.core.orchestrator import OrchestratorState
    from tradingagents.agent_harness.tools import ToolContext
    from tradingagents.agent_harness.harness import Harness, HarnessConfig

    h = Harness(HarnessConfig(data_dir=pathlib.Path("/tmp")))
    orch = h.orchestrator
    state = OrchestratorState(
        session_id="s1",
        user_message="分析 600036 估值",
        symbols=["600036.SS"],
    )
    ctx = ToolContext(session_id="s1")

    async def _run():
        plan1 = await orch._plan(state, ctx)
        plan2 = await orch._plan(state, ctx)
        return plan1, plan2

    plan1, plan2 = asyncio.run(_run())
    assert plan2 == plan1
    assert orch.plan_cache.size == 1
    stats = orch.plan_cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_plan_node_normalizes_whitespace_for_cache_key():
    """Plan reuse condition (N65 fix): identical content with different
    whitespace / punctuation must hit the cache."""
    import asyncio
    import pathlib
    from tradingagents.agent_harness.core.orchestrator import OrchestratorState
    from tradingagents.agent_harness.tools import ToolContext
    from tradingagents.agent_harness.harness import Harness, HarnessConfig

    h = Harness(HarnessConfig(data_dir=pathlib.Path("/tmp")))
    orch = h.orchestrator
    ctx = ToolContext(session_id="s1")

    async def _run():
        state1 = OrchestratorState(
            session_id="s1",
            user_message="  分析  600036  估值！",
            symbols=["600036.SS"],
        )
        plan1 = await orch._plan(state1, ctx)
        state2 = OrchestratorState(
            session_id="s2",
            user_message="分析 600036 估值",
            symbols=["600036.SS"],
        )
        plan2 = await orch._plan(state2, ctx)
        return plan1, plan2

    plan1, plan2 = asyncio.run(_run())
    assert plan2 == plan1
    # 命中 cache → size 不变
    assert orch.plan_cache.size == 1
    assert orch.plan_cache.stats()["hits"] == 1


def test_plan_node_caches_ptc_program_for_multi_symbol():
    """Multi-symbol path returns a PTC program dict — make sure it is
    cached and reused on the next identical query."""
    import asyncio
    import pathlib
    from tradingagents.agent_harness.core.orchestrator import OrchestratorState
    from tradingagents.agent_harness.tools import ToolContext
    from tradingagents.agent_harness.harness import Harness, HarnessConfig

    h = Harness(HarnessConfig(data_dir=pathlib.Path("/tmp")))
    orch = h.orchestrator
    ctx = ToolContext(session_id="s1")

    async def _run():
        state = OrchestratorState(
            session_id="s1",
            user_message="对比 600036 跟 600000",
            symbols=["600036.SS", "600000.SS"],
        )
        plan1 = await orch._plan(state, ctx)
        plan2 = await orch._plan(state, ctx)
        return plan1, plan2

    plan1, plan2 = asyncio.run(_run())
    assert isinstance(plan1, dict) and plan1.get("mode") == "ptc"
    assert plan2 == plan1
    assert orch.plan_cache.size == 1
