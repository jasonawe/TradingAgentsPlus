"""§0.4.25.1 — multi-intent attaches ``display_html`` on every tool_result.

Backend (``_run_multi``) now calls ``_attach_display_html`` before
emitting each ``tool_result`` event, so the frontend multi-intent
bubble can render each section as a friendly card instead of a
pipe-table dump.

Frontend harness.js's multi-intent bubble prefers
``s.result.display_html`` when present and only falls back to
``formatRawResult`` for legacy tools.

These tests pin the contract on both ends.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core.short_circuit import ShortCircuit
from tradingagents.agent_harness.core.tier import Intent, Op, Tier, RouteResult
from tradingagents.agent_harness.tools.base import BaseTool
from tradingagents.agent_harness.tools.permission import PermissionType
from tradingagents.agent_harness.tools.schema import ToolSchema
from tradingagents.agent_harness.tools.context import ToolContext
from tradingagents.agent_harness.tools.registry import ToolRegistry


# ─────────────────────────────────────────────────────────────────
# Test scaffolding: minimal fake tools + registry wiring
# ─────────────────────────────────────────────────────────────────


class QuoteArgs(BaseModel):
    symbol: str


class QuoteResult(BaseModel):
    symbol: str
    price: float
    change: float
    change_pct: float
    provider: str = "yfinance"


class FundamentalsArgs(BaseModel):
    symbol: str


class FundamentalsResult(BaseModel):
    symbol: str
    pe_ratio: float | None = 38.9
    pb_ratio: float | None = 46.1
    provider: str = "yfinance"


class NoteListArgs(BaseModel):
    symbol: str | None = None
    limit: int = 50


class NoteListResult(BaseModel):
    count: int = 0
    preview: str = ""
    summary: str = ""


class FakeTool(BaseTool):
    """§Test-only BaseTool — no schema validation, just returns the canned result."""

    def __init__(self, schema: ToolSchema, canned_factory):
        self.schema = schema
        self._canned_factory = canned_factory

    @property
    def name(self) -> str:
        return self.schema.name

    async def invoke(self, args, context):
        return self._canned_factory(args)


def _schema(name: str, args_schema, result_schema, *, display_view: str | None = None,
            permission: PermissionType = PermissionType.READ) -> ToolSchema:
    metadata = {}
    if display_view is not None:
        metadata["display_view"] = display_view
    return ToolSchema(
        name=name,
        description=f"fake {name}",
        args_schema=args_schema,
        result_schema=result_schema,
        permission=permission.value,
        metadata=metadata,
    )


def _build_mock_registry() -> ToolRegistry:
    """A registry wired with get_quote / get_fundamentals / list_notes /
    get_history, each with display_view metadata, so the
    §0.4.27 metadata-driven intent lookup fires."""
    reg = ToolRegistry()

    reg.add(FakeTool(
        _schema("get_quote", QuoteArgs, QuoteResult, display_view="quote"),
        lambda args: QuoteResult(symbol=args.symbol, price=338.98, change=0.47, change_pct=1.16),
    ))
    reg.add(FakeTool(
        _schema(
            "get_fundamentals", FundamentalsArgs, FundamentalsResult,
            display_view="fundamentals",
        ),
        lambda args: FundamentalsResult(symbol=args.symbol),
    ))
    reg.add(FakeTool(
        _schema("list_notes", NoteListArgs, NoteListResult, display_view="list"),
        lambda args: NoteListResult(count=2, summary="2 条记录",
                                     preview="| id | symbol |\n| n1 | AAPL |"),
    ))
    reg.add(FakeTool(
        _schema("get_history", QuoteArgs, QuoteResult, display_view="history"),
        lambda args: QuoteResult(symbol=args.symbol, price=100.0, change=0.0, change_pct=0.0),
    ))
    reg.add(FakeTool(
        _schema("list_watchlist", NoteListArgs, NoteListResult, display_view="list"),
        lambda args: NoteListResult(count=0, preview="", summary=""),
    ))
    return reg


def _build_multi_route(symbols: list[str], pairs: list[tuple[Intent, Op]]) -> RouteResult:
    return RouteResult(
        intent=pairs[0][0] if pairs else Intent.UNKNOWN,
        tier=Tier.DIRECT,
        symbols=symbols,
        multi_pairs=pairs,
        reason="test multi-intent",
    )


def _collect(sc: ShortCircuit, route: RouteResult, message: str = "") -> list[tuple[str, dict]]:
    """Drain the async iterator returned by ``_run_multi``."""
    out: list[tuple[str, dict]] = []

    async def _drain():
        async for ev in sc._run_multi(route, message, context=ToolContext(session_id="test-session")):
            out.append(ev)

    import asyncio
    asyncio.run(_drain())
    return out


# ─────────────────────────────────────────────────────────────────
# Backend contract — every tool_result carries display_html
# ─────────────────────────────────────────────────────────────────


def test_multi_intent_attach_display_html_for_each_tool_result():
    """Both get_quote and get_fundamentals must come back with display_html."""
    sc = ShortCircuit(registry=_build_mock_registry())
    route = _build_multi_route(
        ["AAPL"],
        [(Intent.QUOTE, Op.READ), (Intent.FUNDAMENTALS, Op.READ)],
    )
    events = _collect(sc, route)
    tool_results = [ev for ev in events if ev[0] == "tool_result"]
    assert len(tool_results) == 2, f"want 2 tool_results, got {len(tool_results)}"

    for tag, body in tool_results:
        result = body["result"]
        assert "display_html" in result, (
            f"{body['name']} missing display_html; payload={result!r}"
        )
        html = result["display_html"]
        assert isinstance(html, str)
        # Every friendly card wrapper class we emit.
        assert html.startswith('<div class="'), (
            f"{body['name']}: display_html should start with <div class=…>, got: {html[:80]}"
        )


def test_multi_intent_quote_card_class_specific():
    """get_quote → quote-card."""
    sc = ShortCircuit(registry=_build_mock_registry())
    route = _build_multi_route(["AAPL"], [(Intent.QUOTE, Op.READ)])
    events = _collect(sc, route)
    pairs = [(b["name"], b["result"]) for t, b in events if t == "tool_result"]
    assert pairs, "expected 1 tool_result"
    name, payload = pairs[0]
    assert name == "get_quote"
    html = payload["display_html"]
    assert html.startswith('<div class="quote-card">')
    assert "AAPL" in html


def test_multi_intent_fundamentals_card_class_specific():
    """get_fundamentals → fundamentals-card."""
    sc = ShortCircuit(registry=_build_mock_registry())
    route = _build_multi_route(["AAPL"], [(Intent.FUNDAMENTALS, Op.READ)])
    events = _collect(sc, route)
    [(name, payload)] = [(b["name"], b["result"]) for t, b in events if t == "tool_result"]
    assert name == "get_fundamentals"
    html = payload["display_html"]
    assert html.startswith('<div class="fundamentals-card">')


def test_multi_intent_list_intent_uses_list_intent():
    """list_notes is in the display_view metadata map (intent='list'),
    so the result is processed (display_html may be plain text but
    the intent lookup still happens)."""
    sc = ShortCircuit(registry=_build_mock_registry())
    route = _build_multi_route(["AAPL"], [(Intent.NOTE, Op.LIST)])
    events = _collect(sc, route)
    [(name, payload)] = [(b["name"], b["result"]) for t, b in events if t == "tool_result"]
    assert name == "list_notes"
    # The list renderer returns plain text — the keys should still be
    # present so the frontend has full payload access. (list classes
    # do NOT match the multi-intent bubble regex, so it'll fall back
    # to formatRawResult, but the data is still attached.)
    assert "count" in payload and payload["count"] == 2
    assert "summary" in payload


def test_multi_intent_failure_does_not_abort_batch():
    """If one tool raises, the next one still runs and gets display_html.

    Setup: build a registry whose ``get_quote`` raises on invoke but
    ``get_fundamentals`` runs cleanly. The multi-intent batch goes
    Quote → Fundamentals, and we expect:
        - tool_call(quote) + warning(quote failed)  (no tool_result for quote)
        - tool_call(fundamentals) + tool_result(fundamentals, display_html)
    """
    reg = ToolRegistry()

    reg.add(FakeTool(
        _schema("get_quote", QuoteArgs, QuoteResult, display_view="quote"),
        # Force an immediate raise inside the coroutine.
        lambda args: (_ for _ in ()).throw(RuntimeError("provider down")),
    ))
    reg.add(FakeTool(
        _schema(
            "get_fundamentals", FundamentalsArgs, FundamentalsResult,
            display_view="fundamentals",
        ),
        lambda args: FundamentalsResult(symbol=args.symbol),
    ))

    sc = ShortCircuit(registry=reg)
    route = _build_multi_route(
        ["AAPL"],
        [(Intent.QUOTE, Op.READ), (Intent.FUNDAMENTALS, Op.READ)],
    )
    events = _collect(sc, route)
    tool_results = [b for t, b in events if t == "tool_result"]
    warnings = [b for t, b in events if t == "warning"]

    # quote raises → warning, no tool_result for get_quote
    quote_warning = [w for w in warnings if w.get("tool") == "get_quote"]
    assert quote_warning, f"expected warning for get_quote; warnings={warnings}"

    # fundamentals runs cleanly → tool_result with display_html
    fund = [r for r in tool_results if r["name"] == "get_fundamentals"]
    assert fund, f"expected tool_result for get_fundamentals; got {[r['name'] for r in tool_results]}"
    assert "display_html" in fund[0]["result"]
    assert fund[0]["result"]["display_html"].startswith('<div class="fundamentals-card">')


# ─────────────────────────────────────────────────────────────────
# Frontend contract — multi-intent bubble prefers display_html
# ─────────────────────────────────────────────────────────────────


def test_frontend_multi_intent_bubble_prefers_display_html():
    """The new branch in harness.js (multi-intent bubble) prefers
    ``s.result.display_html`` over ``formatRawResult(s.result)``.

    Pin the source contract so a regression is caught here, not in
    the browser console."""
    harness_js = Path("web/static/harness.js").read_text()

    # 1. find the multi-intent body block — the variable name we look
    # for is ``cardHtml``. The display_html branch must guard it
    # with a regex test.
    assert "cardHtml" in harness_js, "multi-intent bubble uses cardHtml variable"

    # 2. The condition must check s.result.display_html is a string
    # and the card-class regex matches.
    cond = re.search(
        r"s\.result\s*&&\s*typeof\s+s\.result\.display_html\s*===\s*\"string\"\s*&&\s*/\^<div",
        harness_js,
    )
    assert cond, "multi-intent bubble doesn't guard display_html with typeof + card regex"

    # 3. The fallback to formatRawResult must still exist for legacy tools.
    assert "formatRawResult(s.result || {}" in harness_js, (
        "multi-intent bubble no longer falls back to formatRawResult"
    )


# ─────────────────────────────────────────────────────────────────
# Regression: legacy tools without metadata still get plain text card
# ─────────────────────────────────────────────────────────────────


def test_legacy_tool_without_metadata_falls_back_gracefully():
    """A tool that's registered *without* ``metadata.display_view``
    must not crash ``_attach_display_html`` — it just gets no
    ``display_html`` and the frontend falls back to
    ``formatRawResult``."""
    reg = _build_mock_registry()
    # Register a tool that has NO display_view metadata.
    reg.add(FakeTool(
        _schema("legacy_tool", QuoteArgs, QuoteResult, display_view=None),
        lambda args: QuoteResult(symbol=args.symbol, price=1.0, change=0.0, change_pct=0.0),
    ))
    sc = ShortCircuit(registry=reg)
    out = sc._attach_display_html(
        "legacy_tool",
        {"symbol": "AAPL", "price": 1.0, "change": 0.0, "change_pct": 0.0},
    )
    # Either no display_html is attached (preferred fallback) OR it
    # falls back to the legacy hardcoded _TOOL_INTENT_MAP. Both are
    # acceptable — the contract is "don't crash".
    assert isinstance(out, dict)
    assert out.get("symbol") == "AAPL"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
