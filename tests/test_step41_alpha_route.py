"""Step 41 \u2014 alpha intent routes to compute_alpha_factors when symbol present.

Before:  "算一下 600036.SS \u7684 alpha158 \u56e0\u5b50" fell through to
list_alpha_factors (just the factor name catalogue, no numeric values).

After:   Intent.ALPHA + symbol \u2192 compute_alpha_factors (real values),
         no symbol \u2192 list_alpha_factors (catalogue).
         The ``alpha`` display_view renders the values dict as a markdown
         table so the chat bubble stays scannable.
"""
from __future__ import annotations

import asyncio
import pytest

from tradingagents.agent_harness.core.short_circuit import ShortCircuit
from tradingagents.agent_harness.core.tier import (
    Intent,
    RouteResult,
    Tier,
    fast_route_with_op,
)
from tradingagents.agent_harness.tools.display_view import (
    _render_alpha,
    display_view_for,
)


# ---------------------------------------------------------------------------
# Routing: _tool_for_intent symbol-aware dispatch
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("symbol,expected_tool", [
    ("600036.SS", "compute_alpha_factors"),
    ("AAPL",      "compute_alpha_factors"),
    ("",          "list_alpha_factors"),
])
def test_tool_for_intent_alpha_routing(symbol, expected_tool):
    tool = ShortCircuit._tool_for_intent(Intent.ALPHA, slots=None, symbol=symbol)
    assert tool == expected_tool, (
        f"symbol={symbol!r} expected {expected_tool}, got {tool}"
    )


def test_tool_for_intent_alpha_symbol_from_slot_ignored():
    """§Step 41 \u2014 the symbol arg wins, slots.symbol alone is not enough.

    Defensive: keeps behaviour unambiguous even if a future refactor
    decides to surface ``slots['symbol']`` (we currently don't).
    """
    # No symbol arg, no symbol in slot \u2192 catalogue
    assert ShortCircuit._tool_for_intent(Intent.ALPHA) == "list_alpha_factors"
    # Explicit empty symbol \u2192 catalogue
    assert ShortCircuit._tool_for_intent(Intent.ALPHA, symbol="") == "list_alpha_factors"


def test_other_intents_unchanged():
    """The symbol arg only affects ALPHA \u2014 other intents stay stable."""
    assert ShortCircuit._tool_for_intent(Intent.QUOTE, symbol="x") == "get_quote"
    assert ShortCircuit._tool_for_intent(Intent.NEWS,  symbol="x") == "get_news"


def test_alpha_tier1_classification():
    """fast_route_with_op('alpha158 \u56e0\u5b50') \u2192 Tier.DIRECT + ALPHA."""
    route, op = fast_route_with_op("\u7b97\u4e00\u4e0b 600036.SS \u7684 alpha158 \u56e0\u5b50")
    assert route.tier == Tier.DIRECT
    assert route.intent == Intent.ALPHA
    assert "600036.SS" in route.symbols


# ---------------------------------------------------------------------------
# Rendering: alpha display_view turns {symbol, values} into markdown
# ---------------------------------------------------------------------------
def test_render_alpha_basic():
    payload = {
        "symbol": "600036.SS",
        "values": {"roc_1": 0.0123, "rsi_14": 55.6, "macd_hist": -0.04},
    }
    out = _render_alpha(payload)
    assert "600036.SS" in out
    assert "roc_1" in out
    assert "rsi_14" in out
    assert "55.6" in out
    # Markdown table header
    assert "| factor | value |" in out


def test_render_alpha_empty_values():
    out = _render_alpha({"symbol": "X", "values": {}})
    assert "X" in out
    assert "no factor values" in out


def test_render_alpha_truncates_after_20():
    values = {f"f{i}": float(i) for i in range(30)}
    out = _render_alpha({"symbol": "X", "values": values})
    assert "f0" in out
    assert "f19" in out
    assert "f29" not in out
    assert "10" in out  # truncation marker


def test_display_view_for_alpha():
    """Plumbing: display_view_for(alpha=...) \u2192 _render_alpha."""
    payload = {"symbol": "X", "values": {"a": 1.0}}
    out = display_view_for(payload, intent="alpha")
    assert "X" in out and "a" in out


# ---------------------------------------------------------------------------
# End-to-end: ShortCircuit.run emits tool_call for compute_alpha_factors
# ---------------------------------------------------------------------------
def test_short_circuit_routes_to_compute_alpha():
    """\u00a7Step 41 \u2014 full SSE stream should mention compute_alpha_factors.

    Uses the real default registry (which knows about both alpha tools);
    the result body may be stub-zero values if yfinance can't reach the
    internet \u2014 that's fine, what we assert is the *tool chosen*.
    """
    from tradingagents.agent_harness.tools import ToolContext, ToolRegistry
    from tradingagents.agent_harness.tools import builtin as builtin_mod

    # Spin up a fresh registry and register all builtin tools so the
    # E2E can resolve compute_alpha_factors without touching the
    # module-level default (avoids cross-test pollution).
    registry = ToolRegistry()
    builtin_mod.install_builtin_tools(registry)
    sc = ShortCircuit(registry=registry)
    route = RouteResult(
        tier=Tier.DIRECT,
        intent=Intent.ALPHA,
        symbols=["600036.SS"],
        reason="test",
    )

    events: list[tuple[str, dict]] = []

    async def _collect():
        async for ev in sc.run(route, "alpha158 \u56e0\u5b50", ToolContext(session_id="t41")):
            events.append(ev)

    asyncio.run(_collect())

    tool_calls = [e for e in events if e[0] == "tool_call"]
    assert tool_calls, f"no tool_call emitted; events={events}"
    assert tool_calls[0][1]["name"] == "compute_alpha_factors", (
        f"expected compute_alpha_factors, got {tool_calls[0][1]}"
    )


# ---------------------------------------------------------------------------
# §Step 41 — compute_alpha_factors expands ``factors=None`` to the
# full alpha158 registry.
# ---------------------------------------------------------------------------
def test_compute_alpha_factors_none_expands_to_registry():
    """When callers (Tier 1 short_circuit) pass only ``symbol``,
    ``factors`` should default to the full alpha158 library — not the
    pre-seam stub ``['alpha_001', 'alpha_002']``.
    """
    from tradingagents.agent_harness.tools import builtin as builtin_mod

    args = builtin_mod.ComputeAlphaFactorsArgs(symbol="600036.SS")
    # The new default is ``None`` so ``args.factors`` is falsy.
    assert not args.factors, f"expected empty/None factors, got {args.factors}"

    # And the actual factor library has way more than the stub's 2.
    from tradingagents.dataflows.alpha_factors import list_factors
    names = [s.name for s in list_factors()]
    assert len(names) >= 30, f"alpha158 library too small: {len(names)}"
    for expected in ("roc_1", "rsi_14", "macd", "boll_pct_b"):
        assert expected in names, f"missing factor {expected!r}"


# ---------------------------------------------------------------------------
# §Step 41.2 — strict markdown-table format (no leading whitespace,
# trailing ``|`` on every row, 6dp float rounding).
# ---------------------------------------------------------------------------
def test_render_alpha_strict_table_format():
    """Each row must be a valid markdown table line: ``| ... | ... |``.

    The previous renderer prefixed every data row with two spaces and
    omitted the trailing ``|`` — the result is a markdown blob, not a
    table.  The frontend renderer would silently break out of the
    table, losing the structured layout.
    """
    out = _render_alpha({
        "symbol": "X",
        "values": {"a": 1.0, "b": 2.0, "c": 3.0},
    })
    lines = out.split("\n")
    # First line = summary header (± X · N 个因子...).
    # Lines [1..] = the markdown table itself.
    assert lines[1] == "| factor | value |", lines[1]
    assert lines[2] == "|---|---|---|", lines[2]
    for row in lines[3:]:
        assert not row.startswith(" "), f"leading whitespace: {row!r}"
        assert row.startswith("|"), f"missing leading |: {row!r}"
        assert row.endswith("|"), f"missing trailing |: {row!r}"


def test_render_alpha_rounds_floats_to_6dp():
    """Tiny floats / huge ints stay readable."""
    out = _render_alpha({
        "symbol": "X",
        "values": {"roc": 0.0002462640864279164, "obv": -308733586.0},
    })
    assert "0.000246" in out
    assert "0.0002462640864279164" not in out  # unrounded form banned
    # Float-with-zero-fractional-part should be printed as int
    # ("is integer" form) so the bubble stays readable.
    assert "-308733586.000000" not in out
    assert "-308733586" in out


def test_render_alpha_truncation_row_has_trailing_pipe():
    """The '... | (其他 12 ...)' continuation row must also be a
    valid table line so the renderer keeps the table open.
    """
    big = {f"f{i}": float(i) for i in range(30)}
    out = _render_alpha({"symbol": "X", "values": big})
    last_data_line = out.split("\n")[-1]
    assert last_data_line.startswith("|"), last_data_line
    assert last_data_line.endswith("|"), last_data_line
