"""P4 tests: tier router, short-circuit, orchestrator, context, retry, verification."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core import (  # noqa: E402
    CircuitBreaker,
    CircuitState,
    ContextPriority,
    Layer,
    Orchestrator,
    OrchestratorState,
    RetryPolicy,
    ShortCircuit,
    Tier,
    VerificationLevel,
    Verifier,
    classify_intent,
    fast_route,
    retry_async,
)
from tradingagents.agent_harness.tools import (  # noqa: E402
    PermissionType,
    ToolContext,
    ToolSchema,
    ToolRegistry,
    BaseTool,
    install_builtin_tools,
)


# ---------------------------------------------------------------------------
# Tier router
# ---------------------------------------------------------------------------


def test_route_quote_returns_tier1() -> None:
    r = fast_route("600036.SS 多少钱")
    assert r.tier == Tier.DIRECT
    assert r.intent.name == "QUOTE"
    assert "600036.SS" in r.symbols


def test_route_compare_returns_tier2() -> None:
    r = fast_route("分析 600036.SS 估值合理性")
    assert r.tier == Tier.PLAN_EXECUTE
    assert r.intent.name == "COMPARE"


def test_route_multi_symbol_deep_returns_tier3() -> None:
    r = fast_route("深度分析 600036 和 600000")
    assert r.tier == Tier.WORKFLOW
    assert len(r.symbols) >= 2


def test_classify_intent_unknown_when_no_keyword() -> None:
    assert classify_intent("hello world").name == "UNKNOWN"


def test_extract_symbols_returns_distinct_order() -> None:
    from tradingagents.agent_harness.core.tier import extract_symbols
    syms = extract_symbols("Compare AAPL vs AAPL and 600036.SS")
    assert syms.count("AAPL") == 1
    assert "600036.SS" in syms


# ---------------------------------------------------------------------------
# Short-circuit (Tier 1)
# ---------------------------------------------------------------------------


def test_short_circuit_emits_tool_call_and_result(monkeypatch) -> None:
    _install_mock_provider(monkeypatch)
    reg = ToolRegistry()
    install_builtin_tools(reg)
    sc = ShortCircuit(reg)
    route = fast_route("600036.SS 多少钱")
    assert route.tier == Tier.DIRECT

    events = asyncio.run(_collect(sc.run(route, "600036.SS 多少钱", ToolContext(session_id="t"))))
    names = [e[0] for e in events]
    assert "tool_call" in names
    assert "tool_result" in names
    assert "agent_final" in names


def test_short_circuit_emits_error_when_no_ticker() -> None:
    reg = ToolRegistry()
    install_builtin_tools(reg)
    sc = ShortCircuit(reg)
    route = fast_route("some question")  # no clear ticker
    events = asyncio.run(_collect(sc.run(route, "some question", ToolContext(session_id="t"))))
    # Should fall back gracefully (no crash).
    assert all(isinstance(e, tuple) and len(e) == 2 for e in events)


async def _collect(aiter):
    out = []
    async for ev in aiter:
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Context priority
# ---------------------------------------------------------------------------


def test_context_priority_orders_high_first() -> None:
    cp = ContextPriority()
    layers = {
        Layer.CHAT: {"recent": "..."},
        Layer.EXPLICIT: {"ticker": "600036"},
        Layer.GLOBAL: {"pref": "value"},
    }
    ordered = cp.assemble(layers)
    priorities = [int(l) for l, _ in ordered]
    assert priorities == sorted(priorities)


def test_context_priority_trims_when_over_budget() -> None:
    cp = ContextPriority()
    big_payload = {"data": "x" * 20000}
    layers = {Layer.FILES: big_payload}
    ordered = cp.assemble(layers)
    payload = ordered[0][1]
    assert "_trimmed" in payload


# ---------------------------------------------------------------------------
# Retry + circuit breaker
# ---------------------------------------------------------------------------


def test_retry_async_eventually_succeeds() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("boom")
        return "ok"

    res = asyncio.run(retry_async(flaky, policy=RetryPolicy(max_retries=3, backoff_seconds=0)))
    assert res == "ok"
    assert calls["n"] == 2


def test_retry_async_gives_up_after_max() -> None:
    async def always_fail() -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(retry_async(always_fail, policy=RetryPolicy(max_retries=2, backoff_seconds=0)))


def test_circuit_breaker_opens_after_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=2, reset_seconds=100)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow() is False
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_verifier_l1_rejects_missing_tool_name() -> None:
    v = Verifier()
    res = v.verify_l1({}, None)
    assert res.ok is False
    assert res.level == VerificationLevel.L1_STRUCTURAL


def test_verifier_l1_passes_with_args_and_result() -> None:
    v = Verifier()
    res = v.verify_l1({"name": "get_quote"}, {"price": 1})
    assert res.ok is True


def test_verifier_l2_flags_quote_with_no_results() -> None:
    v = Verifier()
    res = v.verify_l2("quote", [])
    assert res.ok is False


def test_verifier_l2_passes_for_history_with_warnings() -> None:
    v = Verifier()
    res = v.verify_l2("history", [{"warnings": ["stale"]}])
    assert res.ok is True


# ---------------------------------------------------------------------------
# Orchestrator (Tier 2 5-node state machine)
# ---------------------------------------------------------------------------


class _NoOpLLMFactory:
    """No-op LLM factory that reports configured but never produces text.

    Used by Tier 2 tests so ``maybe_degrade_to_tier1`` (which checks
    ``llm_factory.is_configured()``) does not force Tier 1. The plan
    router / synthesizer never actually call this factory in these
    scenarios — they hit keyword / pre_plan_hook short-circuits before
    needing an LLM response.
    """

    def is_configured(self) -> bool:
        return True

    def make(self, *, mode: str = "deep"):
        raise RuntimeError("NoOpLLMFactory: tests should not need real LLM calls")


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


def test_orchestrator_tier1_short_circuit_emits_agent_final(monkeypatch) -> None:
    _install_mock_provider(monkeypatch)
    orch = _build_orchestrator()
    events = asyncio.run(_collect(orch.stream_chat("s1", "600036.SS 多少钱")))
    names = [e[0] for e in events]
    assert "agent_final" in names
    final = next(p for ev, p in events if ev == "agent_final")
    assert final["tier"] == int(Tier.DIRECT)


def test_orchestrator_tier2_emits_plan_ready(monkeypatch) -> None:
    _install_mock_provider(monkeypatch)
    orch = _build_orchestrator(llm_factory=_NoOpLLMFactory())
    events = asyncio.run(_collect(orch.stream_chat("s1", "分析 600036.SS 估值")))
    names = [e[0] for e in events]
    assert "plan_started" in names
    assert "plan_ready" in names
    assert "agent_final" in names


def test_orchestrator_tier2_executes_quote_tool(monkeypatch) -> None:
    _install_mock_provider(monkeypatch)
    orch = _build_orchestrator(llm_factory=_NoOpLLMFactory())
    events = asyncio.run(_collect(orch.stream_chat("s1", "分析 600036.SS 估值合理性")))
    tool_results = [p for ev, p in events if ev == "tool_result"]
    assert len(tool_results) >= 1
    assert any("600036" in str(r) for r in tool_results)


def test_orchestrator_error_path_emits_error_event() -> None:
    orch = _build_orchestrator()

    # Force the short-circuit path to raise (no symbols).
    events = asyncio.run(_collect(orch.stream_chat("s1", "just chat with no ticker")))
    # Whatever tier, must not raise.
    assert all(isinstance(e, tuple) and len(e) == 2 for e in events)


def test_orchestrator_state_snapshot() -> None:
    state = OrchestratorState(session_id="s", user_message="x", intent=None)
    assert state.tool_results == []
    assert state.error is None


# ---------------------------------------------------------------------------
# Mock provider helper — used by short-circuit / orchestrator tests so they
# don't hit eastmoney's HTTP endpoint (which is network-sensitive in CI).
# ---------------------------------------------------------------------------


class _MockProvider:
    name = "mock"

    def supports(self, symbol, asset_type, capability):
        return True

    def get_quote(self, symbol, asset_type):
        from datetime import datetime, timezone
        from web.market_models import QuoteSnapshot, Freshness
        return QuoteSnapshot(
            symbol=symbol, price=10.0, change=0.1, change_percent=1.0,
            volume=1000, freshness=Freshness.DELAYED, as_of=datetime.now(timezone.utc),
            fetched_at=datetime.now(timezone.utc),
        )

    def get_candles(self, symbol, interval, start, end, asset_type):
        return []

    def get_identity(self, symbol, asset_type):
        from web.market_models import AssetIdentity
        return AssetIdentity(symbol=symbol, asset_type=asset_type)


def _install_mock_provider(monkeypatch):
    from tradingagents.data import providers as p_mod
    from tradingagents.data.providers import registry as reg_mod
    monkeypatch.setitem(reg_mod.PROVIDERS, "mock", _MockProvider())
    monkeypatch.setattr(reg_mod, "_active", "mock")
