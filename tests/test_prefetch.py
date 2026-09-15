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
