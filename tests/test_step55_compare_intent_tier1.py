"""§0.4.23 — compare intent Tier 1 short-circuit."""
import pytest
from tradingagents.agent_harness.core.tier import Intent, RouteResult
from tradingagents.agent_harness.tools.builtin import HistoryArgs


def test_intent_compare_exists():
    assert Intent.COMPARE.value == "compare"


def _fake_history_factory():
    """Build a registry whose ``get_history`` returns canned candles.

    Accepts either a HistoryArgs instance or a dict-shaped object that
    exposes ``.symbol`` (the real builtin tool accepts HistoryArgs).
    """
    class FakeSchema:
        name = "get_history"
        args_schema = HistoryArgs

    class FakeTool:
        schema = FakeSchema()
        async def invoke(self, args, context):
            sym = args.symbol if hasattr(args, "symbol") else args.get("symbol")
            return {
                "symbol": sym,
                "interval": "1d",
                "candles": [
                    {"timestamp": "2026-09-20", "close": 100.0 + hash(sym) % 10},
                    {"timestamp": "2026-09-21", "close": 102.0 + hash(sym) % 10},
                    {"timestamp": "2026-09-22", "close": 104.0 + hash(sym) % 10},
                ],
                "provider": "fake",
            }

    class FakeRegistry:
        def get(self, name): return FakeTool()

    return FakeRegistry()


@pytest.mark.asyncio
async def test_short_circuit_compare_renders_compare_card():
    """Multi-symbol compare intent should fan out to ``get_history`` for
    each symbol and emit a ``render_compare_card`` HTML result."""
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    sc = ShortCircuit(registry=_fake_history_factory())
    route = RouteResult(intent=Intent.COMPARE, symbols=["AAPL", "NVDA"], tier=0)
    events = []
    async for ev in sc.run(route, "对比一下 AAPL 和 NVDA", context=None):
        events.append(ev)

    tool_calls = [e for e in events if e[0] == "tool_call"]
    tool_results = [e for e in events if e[0] == "tool_result"]
    finals = [e for e in events if e[0] == "agent_final"]
    assert len(tool_calls) == 2
    assert len(tool_results) == 2
    assert len(finals) == 1
    final_payload = finals[0][1]
    assert final_payload["rendered"] is True
    assert final_payload["result"].startswith("<div class=\"compare-card\">")
    assert "AAPL" in final_payload["result"]
    assert "NVDA" in final_payload["result"]
    assert final_payload["symbols"] == ["AAPL", "NVDA"]


@pytest.mark.asyncio
async def test_short_circuit_compare_falls_back_for_single_symbol():
    """With only 1 symbol, compare should NOT enter _run_compare —
    the regular single-symbol path handles it. We assert that the
    result is *not* a compare-card HTML."""
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    sc = ShortCircuit(registry=_fake_history_factory())
    route = RouteResult(intent=Intent.COMPARE, symbols=["AAPL"], tier=0)
    finals = []
    async for ev in sc.run(route, "看一下 AAPL", context=None):
        if ev[0] == "agent_final":
            finals.append(ev[1])
    # Single-symbol path goes through _build_args + tool.invoke which
    # for our fake tool returns plain dict → display_view_for would
    # emit JSON. The point is it should NOT be the compare card.
    if finals:
        assert not any(
            isinstance(f.get("result"), str) and "compare-card" in f["result"]
            for f in finals
        )
