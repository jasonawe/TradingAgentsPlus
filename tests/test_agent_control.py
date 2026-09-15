"""Q4 / P1-4 — AgentControlBus (steer / inject / whenIdle).

覆盖:
  - steer 排队 / 消费 / peek
  - inject 排队 / 消费 (next_idle / immediate 过滤)
  - whenIdle per-session + global
  - 同步 / 异步 callback 都支持
  - callback 异常隔离
  - cancel_when_idle 注销
  - 多 session 隔离
  - 输入校验(空 message 抛 ValueError,when 不识别抛)
"""
from __future__ import annotations

import asyncio

import pytest


def test_steer_and_consume_roundtrip():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    bus.steer("s1", "switch to fundamentals", author="scheduler")
    bus.steer("s1", "ignore the price")

    assert bus.peek_pending("s1") == {"steers": 2, "injects": 0}
    out = bus.consume_steers("s1")
    assert [m.message for m in out] == ["switch to fundamentals", "ignore the price"]
    assert out[0].author == "scheduler"
    # consumed
    assert bus.peek_pending("s1") == {"steers": 0, "injects": 0}
    assert bus.consume_steers("s1") == []


def test_inject_next_idle_vs_immediate():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    bus.inject("s1", "A", when="immediate")
    bus.inject("s1", "B", when="next_idle")
    bus.inject("s1", "C", when="next_idle")

    next_idle = bus.consume_injects("s1", when="next_idle")
    assert [m.message for m in next_idle] == ["B", "C"]
    # immediate still queued
    immediate = bus.consume_injects("s1", when="immediate")
    assert [m.message for m in immediate] == ["A"]


def test_inject_consume_all_when_filter_none():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    bus.inject("s1", "A")
    bus.inject("s1", "B", when="immediate")
    all_injects = bus.consume_injects("s1")
    assert len(all_injects) == 2


def test_session_isolation():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    bus.steer("s1", "x")
    bus.steer("s2", "y")
    assert [m.message for m in bus.consume_steers("s1")] == ["x"]
    assert [m.message for m in bus.consume_steers("s2")] == ["y"]


def test_input_validation():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    with pytest.raises(ValueError, match="non-empty"):
        bus.steer("s", "")
    with pytest.raises(ValueError, match="non-empty"):
        bus.inject("s", "")
    with pytest.raises(ValueError, match="when"):
        bus.inject("s", "x", when="bogus")


def test_when_idle_per_session_callback():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    fired = []

    def cb(sid, state):
        fired.append((sid, state))

    bus.when_idle(cb, session_id="s1")

    async def _run():
        n = await bus.notify_idle("s1", state={"last_node": "synthesize"})
        return n

    n = asyncio.run(_run())
    assert n == 1
    assert fired == [("s1", {"last_node": "synthesize"})]


def test_when_idle_global_callback_fires_for_any_session():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    fired = []

    def cb(sid, state):
        fired.append(sid)

    bus.when_idle(cb)  # global

    async def _run():
        await bus.notify_idle("s1")
        await bus.notify_idle("s2")

    asyncio.run(_run())
    assert fired == ["s1", "s2"]


def test_when_idle_async_callback():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    fired = []

    async def cb(sid, state):
        await asyncio.sleep(0)
        fired.append(sid)

    bus.when_idle(cb, session_id="s1")

    async def _run():
        await bus.notify_idle("s1")

    asyncio.run(_run())
    assert fired == ["s1"]


def test_when_idle_callback_exception_isolated():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    fired = []

    def bad(sid, state):
        raise RuntimeError("kaboom")

    def good(sid, state):
        fired.append(sid)

    bus.when_idle(bad, session_id="s1")
    bus.when_idle(good, session_id="s1")

    async def _run():
        return await bus.notify_idle("s1")

    n = asyncio.run(_run())
    # both callbacks fired; bad one didn't stop good one
    assert n == 2
    assert fired == ["s1"]


def test_cancel_when_idle_removes_callbacks():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()

    def cb(sid, state):
        pass

    bus.when_idle(cb, session_id="s1")
    bus.when_idle(cb, session_id="s2")
    bus.when_idle(cb)  # global

    assert bus.idle_callback_count() == 3
    # remove only s1 callback
    removed = bus.cancel_when_idle(cb, session_id="s1")
    assert removed == 1
    assert bus.idle_callback_count() == 2
    # remove all remaining
    removed2 = bus.cancel_when_idle(cb)
    assert removed2 == 2
    assert bus.idle_callback_count() == 0


def test_idle_callback_count_by_session():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    bus.when_idle(lambda *a: None, session_id="s1")
    bus.when_idle(lambda *a: None, session_id="s2")
    bus.when_idle(lambda *a: None)  # global

    assert bus.idle_callback_count() == 3
    # s1 sees 1 per-session + 1 global
    assert bus.idle_callback_count("s1") == 2
    assert bus.idle_callback_count("s_unknown") == 1  # only global


def test_steer_returns_queue_depth():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    assert bus.steer("s", "first") == 1
    assert bus.steer("s", "second") == 2


def test_notify_idle_no_callbacks_returns_zero():
    from tradingagents.agent_harness.core.agent_control import AgentControlBus

    bus = AgentControlBus()
    n = asyncio.run(bus.notify_idle("s"))
    assert n == 0
