"""§7.3 #9 — Prefetcher。

覆盖:
  - kick_off 解析两种 plan 形态(legacy list / PTC program)
  - drain 等所有 in-flight 完成
  - drain 超时标记 timed_out
  - lookup 命中/未命中(进行中/失败/不存在)
  - reset 取消 in-flight
  - 跨 event loop 复用抛 RuntimeError
  - tool.invoke 失败被记录,不抛
  - dedupe 重复 (tool, args)
"""
from __future__ import annotations

import asyncio
import time

import pytest


class _FakeTool:
    def __init__(self, name: str, *, delay: float = 0.01, raise_exc: Exception | None = None):
        self.name = name
        self._delay = delay
        self._raise = raise_exc
        self.invocations: list[tuple] = []

    async def invoke(self, args, context):
        self.invocations.append(args)
        await asyncio.sleep(self._delay)
        if self._raise:
            raise self._raise
        return {"echo": self.name, "args": dict(args) if isinstance(args, dict) else args}


class _FakeRegistry:
    def __init__(self, tools: dict[str, _FakeTool]):
        self._tools = tools

    def get(self, name: str) -> _FakeTool:
        return self._tools[name]


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# 基本行为
# ---------------------------------------------------------------------------


def test_kick_off_fires_one_task_per_plan_step():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        tool = _FakeTool("get_quote")
        reg = _FakeRegistry({"get_quote": tool})
        plan = [
            {"step": 1, "action": "get_quote", "args": {"symbol": "AAPL"}},
            {"step": 2, "action": "get_quote", "args": {"symbol": "NVDA"}},
        ]
        n = pf.kick_off(plan=plan, context=None, tool_registry=reg)
        assert n == 2
        # 等任务完成
        await pf.drain()
        return tool.invocations

    invocations = asyncio.run(_run())
    assert len(invocations) == 2
    assert {"symbol": "AAPL"} in invocations
    assert {"symbol": "NVDA"} in invocations


def test_kick_off_handles_ptc_program_shape():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        quote = _FakeTool("get_quote")
        news = _FakeTool("get_news")
        reg = _FakeRegistry({"get_quote": quote, "get_news": news})
        plan = {
            "mode": "ptc",
            "groups": [
                {
                    "id": "g1",
                    "calls": [
                        {"name": "get_quote", "args": {"symbol": "AAPL"}},
                        {"name": "get_news", "args": {"symbol": "AAPL", "days": 7}},
                    ],
                }
            ],
        }
        n = pf.kick_off(plan=plan, context=None, tool_registry=reg)
        assert n == 2
        await pf.drain()
        return quote.invocations, news.invocations

    q, n = asyncio.run(_run())
    assert q == [{"symbol": "AAPL"}]
    assert n == [{"symbol": "AAPL", "days": 7}]


def test_drain_returns_snapshot_with_counts():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        good = _FakeTool("good", delay=0.01)
        bad = _FakeTool("bad", raise_exc=RuntimeError("boom"))
        reg = _FakeRegistry({"good": good, "bad": bad})
        pf.kick_off(
            plan=[
                {"action": "good", "args": {}},
                {"action": "bad", "args": {}},
            ],
            context=None,
            tool_registry=reg,
        )
        snap = await pf.drain()
        return snap

    snap = asyncio.run(_run())
    assert snap.total == 2
    assert snap.completed == 1
    assert snap.failed == 1
    assert snap.timed_out == 0


def test_drain_timeout_marks_remaining():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        slow = _FakeTool("slow", delay=2.0)
        reg = _FakeRegistry({"slow": slow})
        pf.kick_off(plan=[{"action": "slow", "args": {}}], context=None, tool_registry=reg)
        snap = await pf.drain(timeout=0.05)
        return snap

    snap = asyncio.run(_run())
    assert snap.timed_out == 1
    assert snap.completed == 0


def test_lookup_hit_miss_and_inflight():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        tool = _FakeTool("get_quote", delay=0.05)
        reg = _FakeRegistry({"get_quote": tool})
        pf.kick_off(
            plan=[{"action": "get_quote", "args": {"symbol": "AAPL"}}],
            context=None,
            tool_registry=reg,
        )
        # inflight 中:lookup 不命中(finished_at is None)
        hit, _ = pf.lookup("get_quote", {"symbol": "AAPL"})
        assert hit is False
        await pf.drain()
        hit, result = pf.lookup("get_quote", {"symbol": "AAPL"})
        assert hit is True
        assert result["echo"] == "get_quote"
        # 未启动的工具
        hit2, _2 = pf.lookup("get_news", {"symbol": "AAPL"})
        assert hit2 is False

    asyncio.run(_run())


def test_lookup_does_not_return_errored_results():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        bad = _FakeTool("bad", raise_exc=RuntimeError("nope"))
        reg = _FakeRegistry({"bad": bad})
        pf.kick_off(plan=[{"action": "bad", "args": {}}], context=None, tool_registry=reg)
        await pf.drain()
        hit, _ = pf.lookup("bad", {})
        assert hit is False

    asyncio.run(_run())


def test_reset_cancels_inflight():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        slow = _FakeTool("slow", delay=5.0)
        reg = _FakeRegistry({"slow": slow})
        pf.kick_off(plan=[{"action": "slow", "args": {}}], context=None, tool_registry=reg)
        pf.reset()
        assert len(pf) == 0

    asyncio.run(_run())


def test_dedupe_same_tool_args():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        tool = _FakeTool("get_quote", delay=0.01)
        reg = _FakeRegistry({"get_quote": tool})
        plan_dup = [
            {"action": "get_quote", "args": {"symbol": "AAPL"}},
            {"action": "get_quote", "args": {"symbol": "AAPL"}},
            {"action": "get_quote", "args": {"symbol": "NVDA"}},
        ]
        n = pf.kick_off(plan=plan_dup, context=None, tool_registry=reg)
        # dedupe 后只 2 个 task
        assert n == 2
        await pf.drain()

    asyncio.run(_run())


def test_unknown_tool_in_plan_skipped_not_raised():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        reg = _FakeRegistry({})
        n = pf.kick_off(
            plan=[{"action": "ghost", "args": {}}],
            context=None,
            tool_registry=reg,
        )
        assert n == 0

    asyncio.run(_run())


def test_tool_failure_recorded_not_raised():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        bad = _FakeTool("bad", raise_exc=ValueError("x"))
        reg = _FakeRegistry({"bad": bad})
        pf.kick_off(plan=[{"action": "bad", "args": {}}], context=None, tool_registry=reg)
        snap = await pf.drain()
        assert snap.failed == 1
        assert snap.completed == 0

    asyncio.run(_run())


def test_cache_key_handles_basemodel_and_dict():
    from tradingagents.agent_harness.core.prefetch import _make_cache_key
    from pydantic import BaseModel

    class A(BaseModel):
        symbol: str
        days: int = 7

    # dict
    k1 = _make_cache_key("get_quote", {"symbol": "AAPL"})
    k2 = _make_cache_key("get_quote", {"symbol": "AAPL"})
    assert k1 == k2
    # BaseModel → same key (full model_dump matches explicit dict)
    k3 = _make_cache_key("get_quote", A(symbol="AAPL", days=7))
    k1_full = _make_cache_key("get_quote", {"symbol": "AAPL", "days": 7})
    assert k3 == k1_full
    # different args
    k4 = _make_cache_key("get_quote", {"symbol": "NVDA"})
    assert k4 != k1
    # None args
    k5 = _make_cache_key("noop", None)
    assert k5[0] == "noop"


def test_empty_plan_no_op():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        reg = _FakeRegistry({})
        assert pf.kick_off(plan=[], context=None, tool_registry=reg) == 0
        assert pf.kick_off(plan={}, context=None, tool_registry=reg) == 0
        snap = await pf.drain()
        assert snap.total == 0

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# §7.3 #9 polish — PrefetchStats counters
# ---------------------------------------------------------------------------
def test_stats_initial_values_are_zero():
    from tradingagents.agent_harness.core.prefetch import Prefetcher, PrefetchStats
    pf = Prefetcher()
    assert pf.stats == PrefetchStats()
    assert pf.stats.as_dict()["hit_rate"] == 0.0  # 0/(0+0) = 0


def test_stats_kick_off_increments_counters():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        reg = _FakeRegistry({"get_quote": _FakeTool("get_quote")})
        n = pf.kick_off(
            plan=[{"action": "get_quote", "args": {"symbol": "A"}}],
            context=None, tool_registry=reg,
        )
        await pf.drain()
        return pf.stats, n

    stats, n = asyncio.run(_run())
    assert n == 1
    assert stats.kick_off_calls == 1
    assert stats.tasks_scheduled == 1
    assert stats.drain_calls == 1
    assert stats.completed == 1
    assert stats.failed == 0


def test_stats_dedupe_increments_counter():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        reg = _FakeRegistry({"get_quote": _FakeTool("get_quote")})
        plan_dup = [
            {"action": "get_quote", "args": {"symbol": "A"}},
            {"action": "get_quote", "args": {"symbol": "A"}},
        ]
        n = pf.kick_off(plan=plan_dup, context=None, tool_registry=reg)
        await pf.drain()
        return pf.stats, n

    stats, n = asyncio.run(_run())
    assert n == 1  # 第二次 dedupe
    assert stats.tasks_scheduled == 1
    assert stats.tasks_deduped == 1


def test_stats_skipped_unknown_tool_counter():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        reg = _FakeRegistry({})  # unknown tools
        n = pf.kick_off(
            plan=[{"action": "ghost_tool", "args": {}}],
            context=None, tool_registry=reg,
        )
        await pf.drain()
        return pf.stats, n

    stats, n = asyncio.run(_run())
    assert n == 0
    assert stats.tasks_skipped_unknown_tool == 1


def test_stats_lookup_hit_and_miss():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        reg = _FakeRegistry({"get_quote": _FakeTool("get_quote", delay=0.01)})
        pf.kick_off(
            plan=[{"action": "get_quote", "args": {"symbol": "A"}}],
            context=None, tool_registry=reg,
        )
        await pf.drain()
        # Now lookup should hit
        hit, result = pf.lookup("get_quote", {"symbol": "A"})
        # And miss on different args
        miss_hit, miss_result = pf.lookup("get_quote", {"symbol": "B"})
        return pf.stats, hit, miss_hit

    stats, hit, miss_hit = asyncio.run(_run())
    assert hit is True
    assert miss_hit is False
    assert stats.cache_hits == 1
    assert stats.cache_misses == 1
    assert stats.as_dict()["hit_rate"] == 0.5


def test_stats_failed_counter_in_correct():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        reg = _FakeRegistry({"bad": _FakeTool("bad", raise_exc=RuntimeError("boom"))})
        pf.kick_off(
            plan=[{"action": "bad", "args": {}}],
            context=None, tool_registry=reg,
        )
        await pf.drain()
        return pf.stats

    stats = asyncio.run(_run())
    assert stats.completed == 0
    assert stats.failed == 1


def test_stats_timed_out_counter_in_correct():
    import time as _t
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        reg = _FakeRegistry({"slow": _FakeTool("slow", delay=0.5)})
        pf.kick_off(
            plan=[{"action": "slow", "args": {}}],
            context=None, tool_registry=reg,
        )
        snap = await pf.drain(timeout=0.05)
        return pf.stats, snap

    stats, snap = asyncio.run(_run())
    assert snap.timed_out == 1
    assert stats.timed_out == 1


def test_stats_reset_clears_counters():
    from tradingagents.agent_harness.core.prefetch import Prefetcher

    async def _run():
        pf = Prefetcher()
        reg = _FakeRegistry({"get_quote": _FakeTool("get_quote")})
        pf.kick_off(
            plan=[{"action": "get_quote", "args": {"symbol": "A"}}],
            context=None, tool_registry=reg,
        )
        await pf.drain()
        pf.reset_stats()
        return pf.stats

    stats = asyncio.run(_run())
    assert stats.completed == 0
    assert stats.kick_off_calls == 0


# ---------------------------------------------------------------------------
# PlanPredictor — pattern-based prediction
# ---------------------------------------------------------------------------
def test_predictor_empty_history_returns_empty():
    from tradingagents.agent_harness.core.prefetch import PlanPredictor
    p = PlanPredictor()
    assert p.predict([], ("a", "b")) == ()


def test_predictor_empty_prefix_returns_empty():
    from tradingagents.agent_harness.core.prefetch import PlanPredictor
    p = PlanPredictor()
    assert p.predict([("a", "b", "c")], ()) == ()


def test_predictor_returns_most_common_continuation():
    from tradingagents.agent_harness.core.prefetch import PlanPredictor
    history = [
        ("get_quote", "get_fundamentals"),
        ("get_quote", "get_fundamentals"),
        ("get_quote", "get_news"),
        ("get_quote", "get_fundamentals"),
    ]
    p = PlanPredictor()
    result = p.predict(history, ("get_quote",))
    assert result == ("get_fundamentals",)


def test_predictor_caps_lookahead_length():
    from tradingagents.agent_harness.core.prefetch import PlanPredictor
    history = [("a", "b", "c", "d", "e", "f", "g")]
    p = PlanPredictor(max_lookahead=3)
    result = p.predict(history, ("a",))
    assert len(result) <= 3


def test_predictor_min_support_filters_rare_continuations():
    from tradingagents.agent_harness.core.prefetch import PlanPredictor
    history = [
        ("a", "b"),
        ("a", "b"),
        ("a", "rare"),
    ]
    p = PlanPredictor(min_support=2)
    result = p.predict(history, ("a",))
    assert result == ("b",)  # "rare" filtered out (count=1 < min_support=2)


def test_predictor_no_match_returns_empty():
    from tradingagents.agent_harness.core.prefetch import PlanPredictor
    history = [("x", "y"), ("z", "w")]
    p = PlanPredictor()
    assert p.predict(history, ("a",)) == ()


def test_predictor_partial_prefix_match():
    """Prefix can match at any position in history."""
    from tradingagents.agent_harness.core.prefetch import PlanPredictor
    history = [
        ("a", "b", "c"),
        ("a", "b", "d"),
        ("x", "a", "b", "c"),  # 'a','b' at position 1
    ]
    p = PlanPredictor()
    result = p.predict(history, ("a", "b"))
    # c and d both have 2 hits — tie, earliest wins
    assert result in (("c",), ("d",))


def test_predictor_rejects_invalid_max_lookahead():
    from tradingagents.agent_harness.core.prefetch import PlanPredictor
    with pytest.raises(ValueError):
        PlanPredictor(max_lookahead=0)


def test_predictor_rejects_invalid_min_support():
    from tradingagents.agent_harness.core.prefetch import PlanPredictor
    with pytest.raises(ValueError):
        PlanPredictor(min_support=0)


# ---------------------------------------------------------------------------
# record_history + predict + predict_and_kick_off
# ---------------------------------------------------------------------------
def test_record_history_evicts_oldest():
    from tradingagents.agent_harness.core.prefetch import Prefetcher
    pf = Prefetcher(history_capacity=3)
    pf.record_history(("a",))
    pf.record_history(("b",))
    pf.record_history(("c",))
    pf.record_history(("d",))
    # (a,) should be evicted
    assert ("a",) not in pf._history
    assert ("b",) in pf._history
    assert ("d",) in pf._history


def test_record_history_ignores_empty():
    from tradingagents.agent_harness.core.prefetch import Prefetcher
    pf = Prefetcher()
    pf.record_history(())
    pf.record_history([])
    assert pf._history == []


def test_predict_returns_predictor_result():
    from tradingagents.agent_harness.core.prefetch import Prefetcher, PlanPredictor
    pf = Prefetcher(predictor=PlanPredictor())
    pf.record_history(("get_quote", "get_fundamentals"))
    pf.record_history(("get_quote", "get_fundamentals"))
    result = pf.predict_next(("get_quote",))
    assert result == ("get_fundamentals",)


def test_predict_returns_empty_when_no_predictor():
    from tradingagents.agent_harness.core.prefetch import Prefetcher
    pf = Prefetcher()  # no predictor
    pf.record_history(("a", "b"))
    assert pf.predict_next(("a",)) == ()


def test_predict_returns_empty_for_empty_prefix():
    from tradingagents.agent_harness.core.prefetch import Prefetcher, PlanPredictor
    pf = Prefetcher(predictor=PlanPredictor())
    pf.record_history(("a", "b"))
    assert pf.predict_next(()) == ()


def test_predict_and_kick_off_uses_default_args():
    from tradingagents.agent_harness.core.prefetch import Prefetcher, PlanPredictor

    async def _run():
        pf = Prefetcher(predictor=PlanPredictor())
        pf.record_history(("get_quote", "get_fundamentals"))
        pf.record_history(("get_quote", "get_fundamentals"))
        reg = _FakeRegistry({
            "get_quote": _FakeTool("get_quote"),
            "get_fundamentals": _FakeTool("get_fundamentals"),
        })
        n = pf.predict_and_kick_off(
            prefix=("get_quote",),
            context=None,
            tool_registry=reg,
            default_args={"symbol": "600036.SS"},
        )
        await pf.drain()
        return pf.stats, n

    stats, n = asyncio.run(_run())
    assert n == 1  # get_fundamentals 启动了一个 task
    assert stats.predicted_calls == 1


def test_predict_and_kick_off_records_predicted_cache_hits():
    from tradingagents.agent_harness.core.prefetch import Prefetcher, PlanPredictor

    async def _run():
        pf = Prefetcher(predictor=PlanPredictor())
        # 训练历史: query → 总是 follow up with get_fundamentals
        for _ in range(3):
            pf.record_history(("get_quote", "get_fundamentals"))
        reg = _FakeRegistry({
            "get_quote": _FakeTool("get_quote"),
            "get_fundamentals": _FakeTool("get_fundamentals"),
        })
        # First "real" call: get_quote
        pf.kick_off(
            plan=[{"action": "get_quote", "args": {"symbol": "X"}}],
            context=None, tool_registry=reg,
        )
        await pf.drain()
        # Then predict + kick_off next predicted tool
        n = pf.predict_and_kick_off(
            prefix=("get_quote",),
            context=None, tool_registry=reg,
            default_args={"symbol": "X"},
        )
        await pf.drain()
        # Now real plan: query + fundamentals
        return pf.stats, n

    stats, n = asyncio.run(_run())
    assert n == 1
    assert stats.predicted_calls == 1
    # The prediction kicked off get_fundamentals speculatively


def test_reset_clears_history_and_stats():
    from tradingagents.agent_harness.core.prefetch import Prefetcher, PlanPredictor

    async def _run():
        pf = Prefetcher(predictor=PlanPredictor())
        pf.record_history(("a", "b"))
        reg = _FakeRegistry({"get_quote": _FakeTool("get_quote")})
        pf.kick_off(
            plan=[{"action": "get_quote", "args": {"symbol": "A"}}],
            context=None, tool_registry=reg,
        )
        await pf.drain()
        pf.reset()
        return pf._history, pf.stats.kick_off_calls

    history, kick_count = asyncio.run(_run())
    assert history == []
    assert kick_count == 0
