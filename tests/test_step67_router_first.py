"""§0.4.31 — router-first integration tests.

When the LLM router emits a usable plan (source in {"llm", "cache"}
with at least one call), stream_chat must:

- skip the keyword fast_route_with_op path entirely (router wins),
- force Tier.PLAN_EXECUTE so _plan() reuses the router plan via
  state.router_plan (no second LLM round-trip),
- propagate every ticker the router emitted to route.symbols.

When the router is empty / fails / factory disabled, stream_chat must
fall back to the legacy fast_route_with_op keyword path so existing
short_circuit behaviour is preserved (Tier 1 reads still work).

These tests exercise the orchestrator top-level stream_chat so we catch
regressions where router-first is bypassed (which would re-introduce
the keyword-substring-hijack failures that motivated §0.4.31).
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core import (  # noqa: E402
    CircuitBreaker,
    ContextPriority,
    Orchestrator,
    RetryPolicy,
)
from tradingagents.agent_harness.core.llm_router import (  # noqa: E402
    RouterPlan,
    ToolCall,
)
from tradingagents.agent_harness.core.tier import (  # noqa: E402
    Op,
    Tier,
    fast_route_with_op,
)
from tradingagents.agent_harness.tools import (  # noqa: E402
    ToolRegistry,
    install_builtin_tools,
)


# ─────────────────────────────────────────────────────────────────
# Mock LLM plumbing (re-uses the same shape as test_step66)
# ─────────────────────────────────────────────────────────────────


@dataclass
class _MockLLMFactory:
    response: str = '[{"tool": "get_quote", "args": {"symbol": "600036.SS"}}]'
    raise_exc: Exception | None = None
    call_count: int = field(default=0)
    configured: bool = True

    def make(self, *, mode: str = "deep"):
        return _MockProvider(self)

    def is_configured(self) -> bool:
        return self.configured


@dataclass
class _MockProvider:
    factory: _MockLLMFactory

    def complete_text(self, *, prompt, system, temperature=0.0, max_tokens=80):
        self.factory.call_count += 1
        if self.factory.raise_exc:
            raise self.factory.raise_exc
        return _MockResponse(content=self.factory.response)


@dataclass
class _MockResponse:
    content: str


def _build_orchestrator(*, llm_factory=None) -> Orchestrator:
    reg = ToolRegistry()
    install_builtin_tools(reg)
    return Orchestrator(
        tool_registry=reg,
        agent_registry=None,
        llm_factory=llm_factory,
        context_priority=ContextPriority(),
        retry_policy=RetryPolicy(max_retries=1, backoff_seconds=0),
        circuit_breaker=CircuitBreaker(failure_threshold=10, reset_seconds=30),
        audit=None,
    )


def _install_mock_provider(monkeypatch) -> None:
    """Mock data providers so get_quote doesn't hit the network."""
    from tradingagents.data.providers import registry as reg_mod

    class _MockProvider:
        name = "mock"

        def get_quote(self, symbol, **kwargs):
            return {"symbol": symbol, "price": 1.0, "currency": "USD"}

        def get_history(self, symbol, **kwargs):
            return {"symbol": symbol, "candles": []}

    monkeypatch.setitem(reg_mod.PROVIDERS, "mock", _MockProvider())
    monkeypatch.setattr(reg_mod, "_active", "mock")


def _patch_short_circuit(orch: Orchestrator, captured: dict) -> None:
    """Replace ShortCircuit.run with a no-op that records state.

    Without this, Tier.DIRECT (degraded) paths short-circuit before
    _plan is called, so we can't inspect state.router_plan.
    """

    async def fake_run(route, user_message, context, *, slots=None):
        captured.setdefault("short_circuit_route", route)
        # Yield a single agent_final so stream_chat's loop completes.
        yield ("agent_final", {"tier": int(route.tier), "stub": True})

    orch._short_circuit.run = fake_run  # type: ignore[method-assign]


def _patch_plan(orch: Orchestrator, captured: dict) -> None:
    """Replace _plan so we don't execute real tools — capture state."""

    async def fake_plan(state, context):
        captured["router_plan_source"] = state.router_plan.source
        captured["state_intent"] = state.intent
        captured["state_symbols"] = list(state.symbols)
        captured["calls"] = list(state.router_plan.calls or [])
        return []

    orch._plan = fake_plan  # type: ignore[method-assign]


# ─────────────────────────────────────────────────────────────────
# §0.4.31 — router-first contract
# ─────────────────────────────────────────────────────────────────


def test_router_first_with_llm_forces_plan_execute():
    """When the LLM router emits a plan, stream_chat forces PLAN_EXECUTE.

    We assert that state.router_plan is set and the legacy fast_route
    tier is NOT returned (router wins). _plan() is mocked out so we
    don't trigger network / LLM I/O.
    """
    factory = _MockLLMFactory(
        response='[{"tool": "get_quote", "args": {"symbol": "600036.SS"}}]'
    )
    orch = _build_orchestrator(llm_factory=factory)

    captured: dict[str, Any] = {}
    _patch_plan(orch, captured)

    async def main():
        async for _ev, _p in orch.stream_chat("s1", "分析 600036.SS 估值"):
            pass

    asyncio.run(main())

    assert captured, "_plan was never invoked"
    assert captured["router_plan_source"] in ("llm", "cache")
    assert len(captured["calls"]) == 1
    assert captured["calls"][0].tool == "get_quote"


def test_router_first_falls_back_to_fast_route_when_factory_disabled():
    """When llm_factory is None, router returns source="empty" and
    stream_chat MUST fall back to fast_route_with_op.

    Without LLM, maybe_degrade_to_tier1 (§7.2 #1) degrades to Tier.DIRECT,
    so we patch short_circuit to capture state.router_plan BEFORE the
    short_circuit returns.
    """
    orch = _build_orchestrator(llm_factory=None)

    captured: dict[str, Any] = {}
    _patch_short_circuit(orch, captured)

    async def main():
        async for _ev, _p in orch.stream_chat("s1", "600036.SS 多少钱"):
            pass

    asyncio.run(main())

    route = captured.get("short_circuit_route")
    assert route is not None, "short_circuit never ran"
    # Verify the route tier is whatever fast_route_with_op returned
    # (likely DIRECT after §7.2 degrade — but the intent must match).
    fallback_route, _ = fast_route_with_op("600036.SS 多少钱")
    assert route.intent == fallback_route.intent


def test_router_first_llm_exception_does_not_break_stream_chat():
    """LLM raising must NOT bubble out of stream_chat — keyword fallback
    keeps Tier 1 short-circuit working."""
    factory = _MockLLMFactory(raise_exc=RuntimeError("upstream boom"))
    monkeypatch = pytest.MonkeyPatch()
    try:
        _install_mock_provider(monkeypatch)
        orch = _build_orchestrator(llm_factory=factory)

        async def main():
            events = []
            async for ev, _p in orch.stream_chat("s1", "600036.SS 多少钱"):
                events.append(ev)
            return events

        events = asyncio.run(main())
        assert "agent_final" in events, events
    finally:
        monkeypatch.undo()


# ─────────────────────────────────────────────────────────────────
# §0.4.31 — pure helpers (no orchestrator instance required)
# ─────────────────────────────────────────────────────────────────


def test_infer_intent_op_from_router_read_only_single_tool_is_quote():
    """A single read tool should infer (QUOTE, READ)."""
    plan = RouterPlan(
        calls=[ToolCall(tool="get_quote", args={"symbol": "600036.SS"})],
        source="llm",
    )
    intent, op = Orchestrator._infer_intent_op_from_router(plan)
    assert intent.value == "quote"
    assert op.value == "read"


def test_infer_intent_op_from_router_multi_tool_is_analyze_read():
    """2+ distinct read tools infer (ANALYZE, READ)."""
    plan = RouterPlan(
        calls=[
            ToolCall(tool="get_quote", args={"symbol": "600036.SS"}),
            ToolCall(tool="get_news", args={"symbol": "600036.SS"}),
        ],
        source="llm",
    )
    intent, op = Orchestrator._infer_intent_op_from_router(plan)
    assert intent.value == "analysis"
    assert op.value == "read"


def test_infer_intent_op_from_router_write_tool_is_create():
    """Any write tool in the plan bumps op to CREATE (HITL marker)."""
    plan = RouterPlan(
        calls=[
            ToolCall(tool="get_quote", args={"symbol": "600036.SS"}),
            ToolCall(
                tool="create_alert",
                args={"symbol": "600036.SS", "kind": "price_above"},
            ),
        ],
        source="llm",
    )
    intent, op = Orchestrator._infer_intent_op_from_router(plan)
    assert op.value == "create"


def test_extract_router_symbols_collects_symbol_and_symbols_args():
    """All ticker symbols mentioned in any router call must end up on
    route.symbols (otherwise fast_route carry-forward wouldn't see them)."""
    plan = RouterPlan(
        calls=[
            ToolCall(tool="get_quote", args={"symbol": "600036.SS"}),
            ToolCall(
                tool="get_quotes_batch",
                args={"symbols": ["600000.SS", "600036.SS"]},
            ),
            ToolCall(tool="get_news", args={"symbol": "688825.SS"}),
        ],
        source="llm",
    )
    syms = Orchestrator._extract_router_symbols(plan)
    assert "600036.SS" in syms
    assert "600000.SS" in syms
    assert "688825.SS" in syms
    # Order-preserving + dedup.
    assert len(syms) == len(set(syms))


def test_extract_router_symbols_handles_missing_or_empty_args():
    """Defensive: malformed router args (None, missing keys) shouldn't
    crash extraction."""
    plan = RouterPlan(
        calls=[ToolCall(tool="get_news", args={})],
        source="llm",
    )
    assert Orchestrator._extract_router_symbols(plan) == []


# ─────────────────────────────────────────────────────────────────
# §0.4.31 — router cache hit short-circuits the LLM
# ─────────────────────────────────────────────────────────────────


def test_router_cache_hit_avoids_second_llm_call():
    """Same user message twice → _llm_router cache hit, count not
    incremented beyond the first turn.

    Note: §0.4.31 keeps _intent_router (§0.4.29) running for diagnostic
    metrics, so the per-turn floor is 2 LLM calls (intent router + plan
    router) the first turn, 1 the second (intent router only — plan router
    hits its own cache). The router cache contract under test is that the
    _llm_router side stops growing beyond its first call.
    """
    factory = _MockLLMFactory(
        response='[{"tool": "get_quote", "args": {"symbol": "600036.SS"}}]'
    )
    orch = _build_orchestrator(llm_factory=factory)

    async def fake_plan(state, context):
        return []

    orch._plan = fake_plan  # type: ignore[method-assign]

    async def run_once():
        async for _ in orch.stream_chat("s1", "分析 600036.SS 估值"):
            pass

    asyncio.run(run_once())
    # Before the second run, record the router-side call count by
    # inspecting orch._llm_router.stats.
    plan_router_stats_first = orch._llm_router.stats.copy()
    asyncio.run(run_once())
    plan_router_stats_second = orch._llm_router.stats.copy()

    # _llm_router.llm_calls grew by 0 the second turn (cache hit).
    assert plan_router_stats_second["llm_calls"] == plan_router_stats_first["llm_calls"], (
        f"_llm_router LLM call grew across runs: {plan_router_stats_first} -> {plan_router_stats_second}"
    )
    assert plan_router_stats_second["cache_hits"] >= plan_router_stats_first["cache_hits"] + 1, (
        f"expected cache hit on 2nd run, stats: {plan_router_stats_second}"
    )
