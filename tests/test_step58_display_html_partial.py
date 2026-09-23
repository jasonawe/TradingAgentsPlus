"""§0.4.24 — error card retry button (frontend helper)
   §0.4.25 — backend attaches display_html on tool_result
   §0.4.26 — get_history returns empty result instead of raising on NO_DATA"""

# §0.4.25 — backend display_html injection
from tradingagents.agent_harness.core.short_circuit import ShortCircuit


def test_short_circuit_attaches_display_html_for_get_quote():
    sc = ShortCircuit(registry=None)
    out = sc._attach_display_html("get_quote", {
        "symbol": "AAPL", "price": 338.98, "change": 0.47,
        "change_pct": 1.16, "currency": "USD", "provider": "yfinance",
    })
    assert "display_html" in out
    assert out["display_html"].startswith("<div class=\"quote-card\">")


def test_short_circuit_attaches_display_html_for_error():
    sc = ShortCircuit(registry=None)
    out = sc._attach_display_html("get_history", {
        "error": "no_data", "error_code": "no_data", "symbol": "AAPL",
    })
    assert "display_html" in out
    assert out["display_html"].startswith("<div class=\"error-card\">")
    assert "暂无数据" in out["display_html"]


def test_short_circuit_unknown_tool_falls_back_gracefully():
    """Tools not in the intent map (e.g. legacy tools) shouldn't crash."""
    sc = ShortCircuit(registry=None)
    out = sc._attach_display_html("some_unknown_tool", {"x": 1})
    # Without intent, display_html not attached — but no exception.
    assert "display_html" not in out


def test_short_circuit_non_dict_payload_passes_through():
    sc = ShortCircuit(registry=None)
    assert sc._attach_display_html("get_quote", None) is None
    assert sc._attach_display_html("get_quote", [1, 2, 3]) == [1, 2, 3]


def test_short_circuit_tool_intent_map_covers_read_tools():
    """Sanity: every Tier 1 read tool is mapped."""
    sc = ShortCircuit(registry=None)
    for tool in ("get_quote", "get_history", "get_fundamentals", "get_news",
                 "list_alpha_factors", "list_reports", "list_watchlist",
                 "list_notes", "list_alerts", "list_scheduled_tasks",
                 "get_report", "add_watchlist", "remove_watchlist"):
        assert tool in sc._TOOL_INTENT_MAP, f"{tool} missing from intent map"


# §0.4.26 — get_history partial-data semantics
import asyncio
from tradingagents.agent_harness.tools.builtin import get_history, HistoryArgs


def test_get_history_empty_candles_returns_gracefully():
    """When every provider returns NO_DATA, get_history should return an
    empty result (not raise) so the friendly renderer shows a partial-
    data card instead of an error card."""

    async def run():
        # Use a clearly-invalid symbol that triggers NO_DATA across all
        # providers in the failover chain.
        args = HistoryArgs(symbol="ABCDE_NOTREAL_XYZ", interval="1d",
                           lookback_days=30)
        r = await get_history(args)
        assert r.candles == []
        assert r.symbol == "ABCDE_NOTREAL_XYZ"

    asyncio.run(run())


def test_render_history_card_empty_friendly_hint():
    """render_history_card must produce a friendly empty-state card
    that includes a hint about why the data is missing."""
    from tradingagents.agent_harness.renderers.history_sparkline import (
        render_history_card,
    )
    html = render_history_card({
        "symbol": "ABCDE", "interval": "1d", "candles": [],
        "provider": "eastmoney",
    })
    assert "history-card empty" in html
    assert "暂无历史数据" in html
    assert "刚上市" in html or "数据源未覆盖" in html
    assert "eastmoney" in html
