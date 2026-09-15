"""Tests for §7.2 #1 — LLM-unavailable degrade to Tier 1 short circuit.

覆盖:
- maybe_degrade_to_tier1 helper: 不同 LLM 状态下行为
- orchestrator.stream_chat 集成: degraded → short_circuit 路径,emit warning
"""
from __future__ import annotations

import asyncio
import pathlib
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# maybe_degrade_to_tier1 helper
# ---------------------------------------------------------------------------
def _make_route(tier, intent="compare", symbols=None, reason="kw match"):
    from tradingagents.agent_harness.core.tier import Intent, RouteResult, Tier
    tier_map = {"direct": Tier.DIRECT, "plan": Tier.PLAN_EXECUTE, "workflow": Tier.WORKFLOW}
    intent_map = {i.value: i for i in Intent}
    return RouteResult(
        intent=intent_map.get(intent, Intent.COMPARE),
        tier=tier_map[tier],
        symbols=symbols or [],
        confidence=0.7,
        reason=reason,
    )


class _State:
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class _FakeBreaker:
    def __init__(self, state):
        self.state = state


def test_degrade_already_tier1_is_noop():
    from tradingagents.agent_harness.core.tier import maybe_degrade_to_tier1
    route = _make_route("direct", symbols=["600036.SS"])
    out, degraded = maybe_degrade_to_tier1(route, llm_factory=None, circuit_breaker=None)
    assert degraded is False
    assert out is route


def test_degrade_no_symbols_keeps_route():
    from tradingagents.agent_harness.core.tier import maybe_degrade_to_tier1
    route = _make_route("plan", symbols=[])
    out, degraded = maybe_degrade_to_tier1(route, llm_factory=None, circuit_breaker=None)
    assert degraded is False
    assert out is route


def test_degrade_llm_factory_none_triggers_fallback():
    from tradingagents.agent_harness.core.tier import Intent, Tier, maybe_degrade_to_tier1
    route = _make_route("plan", intent="compare", symbols=["600036.SS"])
    out, degraded = maybe_degrade_to_tier1(route, llm_factory=None, circuit_breaker=None)
    assert degraded is True
    assert out.tier == Tier.DIRECT
    assert out.intent == Intent.QUOTE
    assert out.symbols == ["600036.SS"]
    assert "no LLM wired" in out.reason


def test_degrade_llm_factory_not_configured_triggers_fallback():
    from tradingagents.agent_harness.core.tier import Tier, maybe_degrade_to_tier1

    class _Factory:
        def is_configured(self):
            return False

    route = _make_route("plan", symbols=["AAPL"])
    out, degraded = maybe_degrade_to_tier1(route, llm_factory=_Factory(), circuit_breaker=None)
    assert degraded is True
    assert out.tier == Tier.DIRECT


def test_degrade_circuit_open_triggers_fallback():
    from tradingagents.agent_harness.core.tier import Tier, maybe_degrade_to_tier1

    class _Factory:
        def is_configured(self):
            return True

    route = _make_route("plan", symbols=["AAPL"])
    breaker = _FakeBreaker(_State.OPEN)
    out, degraded = maybe_degrade_to_tier1(route, llm_factory=_Factory(), circuit_breaker=breaker)
    assert degraded is True
    assert out.tier == Tier.DIRECT
    assert "circuit open" in out.reason


def test_degrade_circuit_closed_no_fallback():
    from tradingagents.agent_harness.core.tier import maybe_degrade_to_tier1

    class _Factory:
        def is_configured(self):
            return True

    route = _make_route("plan", symbols=["AAPL"])
    breaker = _FakeBreaker(_State.CLOSED)
    out, degraded = maybe_degrade_to_tier1(route, llm_factory=_Factory(), circuit_breaker=breaker)
    assert degraded is False
    assert out is route


def test_degrade_keeps_original_reason_in_chain():
    from tradingagents.agent_harness.core.tier import maybe_degrade_to_tier1
    route = _make_route("plan", symbols=["AAPL"], reason="valuation analysis")
    out, degraded = maybe_degrade_to_tier1(route, llm_factory=None, circuit_breaker=None)
    assert degraded is True
    assert "valuation analysis" in out.reason
    assert "Tier 1 fallback" in out.reason


def test_degrade_factory_is_configured_raises_treated_as_not_configured():
    """If ``is_configured`` itself raises (provider bug), treat as not
    configured and degrade.  Tests defensive try/except in the helper."""
    from tradingagents.agent_harness.core.tier import Tier, maybe_degrade_to_tier1

    class _BuggyFactory:
        def is_configured(self):
            raise RuntimeError("provider probe failed")

    route = _make_route("plan", symbols=["AAPL"])
    out, degraded = maybe_degrade_to_tier1(
        route, llm_factory=_BuggyFactory(), circuit_breaker=None,
    )
    assert degraded is True
    assert out.tier == Tier.DIRECT


# ---------------------------------------------------------------------------
# orchestrator.stream_chat integration
# ---------------------------------------------------------------------------
def _harness_with_no_llm(tmp_path: pathlib.Path):
    """Build a Harness whose llm_factory is None so degradation triggers."""
    from tradingagents.agent_harness.harness import Harness, HarnessConfig
    h = Harness(HarnessConfig(data_dir=tmp_path))
    h.orchestrator.llm_factory = None
    return h


def _harness_with_llm_factory_not_configured(tmp_path: pathlib.Path):
    from tradingagents.agent_harness.harness import Harness, HarnessConfig

    class _Factory:
        def is_configured(self):
            return False

    h = Harness(HarnessConfig(data_dir=tmp_path))
    h.orchestrator.llm_factory = _Factory()
    return h


def _events(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    return {"warning": [p for ev, p in events if ev == "warning"],
            "agent_final": [p for ev, p in events if ev == "agent_final"],
            "tool_call": [p for ev, p in events if ev == "tool_call"],
            "tool_result": [p for ev, p in events if ev == "tool_result"],
            "error": [p for ev, p in events if ev == "error"]}


def test_stream_chat_degrades_to_short_circuit_when_no_llm(tmp_path: pathlib.Path):
    """When llm_factory is None and the query would normally be Tier 2
    (e.g. '估值 600036'), the orchestrator MUST route through the
    Tier 1 short-circuit instead of trying to call the LLM."""
    h = _harness_with_no_llm(tmp_path)
    events = []
    async def _drain():
        async for ev, payload in h.orchestrator.stream_chat("s1", "估值 600036"):
            events.append((ev, payload))
    asyncio.run(_drain())
    summary = _events(events)
    # warning should mention Tier 1 fallback
    assert any("Tier 1 fallback" in w.get("message", "") for w in summary["warning"])
    # short_circuit should still emit a tool_call for get_quote
    assert len(summary["tool_call"]) >= 1
    # and a final answer
    assert len(summary["agent_final"]) >= 1


def test_stream_chat_no_degrade_when_llm_ok(tmp_path: pathlib.Path):
    """When llm_factory is configured and circuit is closed, the
    orchestrator proceeds with the normal Tier 2 path (no warning)."""
    from tradingagents.agent_harness.harness import Harness, HarnessConfig

    class _Factory:
        def is_configured(self):
            return True

    h = Harness(HarnessConfig(data_dir=tmp_path))
    h.orchestrator.llm_factory = _Factory()

    captured: list[tuple[str, dict]] = []

    async def fake_plan(self, state, context):
        return [{"step": 1, "action": "get_quote", "args": {"symbol": "600036.SS"}}]

    async def fake_execute(self, state, context):
        return [{"name": "get_quote", "result": {"price": 41.7}}]

    def fake_observe(self, state):
        return {"items": []}

    async def fake_verify(self, state):
        from tradingagents.agent_harness.core.verification import VerificationResult
        return VerificationResult(True, 0, "ok")

    async def fake_synthesize(self, state):
        return "answer"

    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    originals = {
        "_plan": Orchestrator._plan,
        "_execute": Orchestrator._execute,
        "_observe": Orchestrator._observe,
        "_verify": Orchestrator._verify,
        "_synthesize": Orchestrator._synthesize,
    }
    Orchestrator._plan = fake_plan
    Orchestrator._execute = fake_execute
    Orchestrator._observe = fake_observe
    Orchestrator._verify = fake_verify
    Orchestrator._synthesize = fake_synthesize
    try:
        async def _drain():
            async for ev, payload in h.orchestrator.stream_chat("s1", "估值 600036"):
                captured.append((ev, payload))
        asyncio.run(_drain())
    finally:
        for name, fn in originals.items():
            setattr(Orchestrator, name, fn)

    summary = _events(captured)
    # No "Tier 1 fallback" warning when LLM is OK
    assert not any("Tier 1 fallback" in w.get("message", "") for w in summary["warning"])
