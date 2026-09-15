"""Q4 / P1-4 — Orchestrator 集成 steer / inject / whenIdle。

覆盖:
  - Orchestrator.control_bus 持有 AgentControlBus
  - stream_chat 入口消费 steers 并合并到 user_message
  - stream_chat 入口消费 next_idle injects 并合并
  - immediate inject 保留在队列(stream_chat 不消费)
  - stream_chat 退出时 notify_idle 触发 whenIdle callbacks
  - 异常退出也 notify_idle(try/finally)
  - 跨 orchestrator 实例 bus 隔离(独立实例不共享)
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def harness(tmp_path):
    from tradingagents.agent_harness.harness import Harness, HarnessConfig
    return Harness(HarnessConfig(data_dir=tmp_path))


@pytest.fixture
def patched_plan():
    """Temporarily replace ``Orchestrator._plan`` so tests can short-circuit
    stream_chat without invoking the real plan logic. Always restores the
    original method, even if the test body raises.
    """
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    original = Orchestrator._plan
    yield Orchestrator
    Orchestrator._plan = original


def test_orchestrator_holds_control_bus(harness):
    from tradingagents.agent_harness.core.agent_control import AgentControlBus
    assert isinstance(harness.orchestrator.control_bus, AgentControlBus)


def test_steer_gets_prepended_to_user_message(harness, patched_plan):
    bus = harness.orchestrator.control_bus
    bus.steer("sessA", "switch to fundamentals", author="sched")
    bus.steer("sessA", "ignore price")
    assert bus.peek_pending("sessA")["steers"] == 2

    captured = {}

    async def fake_plan(self, state, context):
        captured["user_message"] = state.user_message
        return []

    patched_plan._plan = fake_plan

    async def _drain():
        async for ev in harness.orchestrator.stream_chat("sessA", "分析 AAPL 估值"):
            pass

    asyncio.run(_drain())

    msg = captured["user_message"]
    assert "[STEERED by sched] switch to fundamentals" in msg
    assert "[STEERED by external] ignore price" in msg
    assert msg.endswith("分析 AAPL 估值")
    assert bus.peek_pending("sessA")["steers"] == 0


def test_next_idle_injects_prepended_to_user_message(harness, patched_plan):
    bus = harness.orchestrator.control_bus
    bus.inject("sessB", "remember: prefer eps_basic", when="next_idle")
    bus.inject("sessB", "ignored", when="immediate")

    captured = {}

    async def fake_plan(self, state, context):
        captured["user_message"] = state.user_message
        return []

    patched_plan._plan = fake_plan

    async def _drain():
        async for ev in harness.orchestrator.stream_chat("sessB", "分析 NVDA"):
            pass

    asyncio.run(_drain())

    msg = captured["user_message"]
    assert "[INJECT from external] remember: prefer eps_basic" in msg
    assert bus.peek_pending("sessB")["injects"] == 1  # immediate 还在


def test_when_idle_callback_fires_on_normal_exit(harness, patched_plan):
    fired = []
    bus = harness.orchestrator.control_bus
    bus.when_idle(lambda sid, state: fired.append(sid), session_id="sessC")

    async def fake_plan(self, state, context):
        return []

    patched_plan._plan = fake_plan

    async def _drain():
        async for ev in harness.orchestrator.stream_chat("sessC", "分析 TSLA"):
            pass

    asyncio.run(_drain())
    assert fired == ["sessC"]


def test_when_idle_callback_fires_on_error(harness, patched_plan):
    fired = []
    bus = harness.orchestrator.control_bus
    bus.when_idle(lambda sid, state: fired.append(sid), session_id="sessD")

    async def boom(self, state, context):
        raise RuntimeError("simulated plan failure")

    patched_plan._plan = boom

    async def _drain():
        try:
            async for ev in harness.orchestrator.stream_chat("sessD", "分析 GOOG"):
                pass
        except RuntimeError:
            pass

    asyncio.run(_drain())
    assert fired == ["sessD"]


def test_two_orchestrator_instances_have_independent_buses(tmp_path):
    """不同 Orchestrator 实例的 bus 隔离(per-instance)。"""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator

    orch1 = Orchestrator(
        tool_registry=type("R", (), {"get": lambda *a: None, "list_names": lambda self: []})(),
        agent_registry=type("R", (), {"list": lambda self: []})(),
        llm_factory=None,
        context_priority=None,
        retry_policy=None,
        circuit_breaker=None,
        audit=None,
    )
    orch2 = Orchestrator(
        tool_registry=type("R", (), {"get": lambda *a: None, "list_names": lambda self: []})(),
        agent_registry=type("R", (), {"list": lambda self: []})(),
        llm_factory=None,
        context_priority=None,
        retry_policy=None,
        circuit_breaker=None,
        audit=None,
    )
    orch1.control_bus.steer("x", "msg-from-orch1")
    assert orch2.control_bus.peek_pending("x") == {"steers": 0, "injects": 0}
    assert orch1.control_bus.peek_pending("x")["steers"] == 1
