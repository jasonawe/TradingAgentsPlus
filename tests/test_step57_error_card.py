"""§0.4.22.fix — error card for no_data / provider_error etc."""
from tradingagents.agent_harness.renderers.friendly_cards import (
    render_error_card, render_error_card_from_tool_result,
)
from tradingagents.agent_harness.tools.display_view import display_view_for


def test_error_card_basic_shape():
    html = render_error_card({"error": "no_data", "symbol": "AAPL"}, "get_history")
    assert html.startswith("<div class=\"error-card\">")
    assert "⚠️" in html
    assert "get_history" in html
    assert "AAPL" in html


def test_error_card_no_data_label():
    html = render_error_card({"error": "no_data: candles unavailable", "error_code": "no_data"})
    assert "暂无数据" in html


def test_error_card_provider_error_label():
    html = render_error_card({"error": "broken", "error_code": "provider_error"})
    assert "数据源异常" in html


def test_error_card_unknown_code_falls_back_to_raw():
    html = render_error_card({"error": "something bad"})
    assert "something bad" in html


def test_error_card_from_tool_result_shape():
    """§0.4.22.fix — accept the harness SSE payload shape (result.error)."""
    payload = {
        "name": "get_history",
        "result": {"error": "no_data", "error_code": "no_data", "symbol": "AAPL"},
    }
    html = render_error_card_from_tool_result("get_history", payload)
    assert "AAPL" in html
    assert "暂无数据" in html


def test_error_card_from_tool_result_top_level_shape():
    payload = {"error": "no_data", "error_code": "no_data", "symbol": "TSLA"}
    html = render_error_card_from_tool_result("get_quote", payload)
    assert "TSLA" in html


def test_display_view_for_returns_error_card_on_no_data():
    """display_view_for must short-circuit to error-card when result.error
    is set, regardless of intent."""
    r = {"error": "no_data: historical candles unavailable",
         "error_code": "no_data", "symbol": "AAPL"}
    html = display_view_for(r, intent="history")
    assert html.startswith("<div class=\"error-card\">")
    assert "暂无数据" in html
    assert "AAPL" in html


def test_display_view_for_returns_error_card_on_provider_error():
    r = {"error": "akshare not implemented", "error_code": "provider_error",
         "symbol": "600036.SS"}
    html = display_view_for(r, intent="quote")
    assert html.startswith("<div class=\"error-card\">")
    assert "数据源异常" in html


def test_display_view_for_no_error_renders_normal_card():
    """Regression: without error, the normal renderer still fires."""
    r = {"symbol": "AAPL", "name": "Apple", "price": 100.0, "currency": "USD"}
    html = display_view_for(r, intent="quote")
    assert html.startswith("<div class=\"quote-card\">")
    assert "AAPL" in html
