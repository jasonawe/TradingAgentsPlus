"""Spec Step 17+18 — report_id slot routing & symbol-less Tier 1.

Step 17: ``extract_slots`` accepts both ``run-<hex>`` and ``report-<id>``
token shapes. Without this, the harness's report_id token
(``report-<short>``) falls through to entity detection and gets
mis-routed.

Step 18: ``short_circuit.run()`` and ``orchestrator.stream_chat()``
allow symbol-less queries to take the Tier 1 path when a
``report_id`` slot is present. The query "读报告 run-55464f..."
must hit ``get_report`` (not Tier 2) without emitting
"no ticker detected" warning.
"""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.core.tier import (
    Intent,
    Op,
    extract_slots,
    fast_route_with_op,
    classify,
)


# ---------------------------------------------------------------------------
# Step 17 — extract_slots accepts run-* and report-* shapes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg,expected", [
    ("看一下 run-55464f38279f4b36a8abc775a3a8f108",
     "run-55464f38279f4b36a8abc775a3a8f108"),
    ("读报告 run-abcdef0123456789",
     "run-abcdef0123456789"),
    ("deep-read report-abc123",
     "report-abc123"),
    ("打开 report-short-xyz",
     "report-short-xyz"),
])
def test_extract_slots_report_id_shapes(msg, expected):
    slots = extract_slots(msg)
    assert slots.get("report_id") == expected


def test_extract_slots_no_token():
    slots = extract_slots("随便看看行情")
    assert "report_id" not in slots


# ---------------------------------------------------------------------------
# Step 17 — classify promotes report_id slot to (REPORT, READ)
# ---------------------------------------------------------------------------
def test_classify_report_id_slot_routes_to_report_read():
    intent, op = classify("读报告 run-55464f38279f4b36a8abc775a3a8f108")
    assert intent == Intent.REPORT
    assert op == Op.READ


def test_classify_report_token_shape_routes_to_report_read():
    intent, op = classify("打开 report-abc123")
    assert intent == Intent.REPORT
    assert op == Op.READ


# ---------------------------------------------------------------------------
# Step 18 — symbol-less Tier 1 path is reachable when report_id slot is set
# ---------------------------------------------------------------------------
def test_fast_route_report_id_with_symbol():
    route, op = fast_route_with_op(
        "看一下 600036.SS 的 run-55464f38279f4b36a8abc775a3a8f108 报告",
    )
    # Symbol present → Tier.DIRECT (single-intent QUOTE/READ style).
    assert op == Op.READ
    # intent classification should be REPORT (because report_id slot wins).
    assert route.intent == Intent.REPORT


def test_fast_route_report_id_only_no_symbol_falls_back_to_tier2():
    route, op = fast_route_with_op(
        "读报告 run-55464f38279f4b36a8abc775a3a8f108",
    )
    # No symbol but report_id slot is present. This must NOT crash and
    # the orchestrator stream_chat path will check for the slot and
    # allow Tier 1. fast_route itself only signals intent+op+reason.
    assert route.intent == Intent.REPORT
    assert op == Op.READ


# ---------------------------------------------------------------------------
# Step 18 — short_circuit.run() accepts symbol-less + report_id slot
# ---------------------------------------------------------------------------
def test_build_args_symbol_less_report_id():
    """_build_args must build a GetReportArgs from slots alone when no
    symbol is present."""
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    from tradingagents.agent_harness.tools.builtin import GetReportArgs

    args = ShortCircuit._build_args(
        GetReportArgs,
        "",
        slots={"report_id": "run-55464f38279f4b36a8abc775a3a8f108"},
    )
    assert args.report_id == "run-55464f38279f4b36a8abc775a3a8f108"


def test_build_args_symbol_less_no_slot_falls_through():
    """Without slots, _build_args still works (legacy behavior)."""
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    from tradingagents.agent_harness.tools.builtin import QuoteArgs

    args = ShortCircuit._build_args(QuoteArgs, "600036.SS")
    assert args.symbol == "600036.SS"


def test_short_circuit_symbol_less_with_report_id_routes_to_get_report():
    """End-to-end: short_circuit.run() with no symbol but report_id
    slot must hit get_report and emit agent_final — not the
    'no ticker detected' warning."""
    import asyncio
    from tradingagents.agent_harness.core.short_circuit import ShortCircuit
    from tradingagents.agent_harness.core.tier import RouteResult, Intent, Op, Tier
    from tradingagents.agent_harness.tools import (
        ToolContext, ToolRegistry, install_builtin_tools,
    )

    reg = ToolRegistry()
    install_builtin_tools(reg)
    sc = ShortCircuit(reg)
    route = RouteResult(
        tier=Tier.DIRECT,
        intent=Intent.REPORT,
        op=Op.READ,
        symbols=[],
        reason="Step 18 fixture",
    )

    async def _collect():
        out = []
        async for ev in sc.run(
            route, "读报告 run-55464f38279f4b36a8abc775a3a8f108",
            ToolContext(session_id="t18"),
            slots={"report_id": "run-55464f38279f4b36a8abc775a3a8f108"},
        ):
            out.append(ev)
        return out

    events = asyncio.run(_collect())
    names = [e[0] for e in events]
    # Should NOT emit "no ticker detected" — that's the regression we
    # are guarding against.
    for name, payload in events:
        if name == "warning":
            assert "no ticker" not in payload.get("message", ""), (
                f"unexpected symbol-less warning: {payload}"
            )
    # Should have invoked get_report.
    tool_calls = [p for n, p in events if n == "tool_call"]
    assert any(p.get("name") == "get_report" for p in tool_calls), (
        f"expected get_report tool_call, got {[p.get('name') for p in tool_calls]}"
    )
