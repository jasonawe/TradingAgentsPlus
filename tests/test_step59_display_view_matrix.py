"""§0.4.28 — display_view_for test matrix.

Every intent that maps to a friendly card (quote / fundamentals /
history / news / alpha / ack / error) should be covered by at least
2 payload shapes (success + error / partial / empty). Plain-text
intents (list / report_list / *_list) are not card-wrapped, so we
verify they still return *something* instead of crashing.
"""
from __future__ import annotations

import re

import pytest

from tradingagents.agent_harness.tools.display_view import display_view_for


# ─────────────────────────────────────────────────────────────────
# Card-wrapped intents — must start with the matching card class.
# ─────────────────────────────────────────────────────────────────

def test_intent_quote_success_starts_with_quote_card():
    """A complete quote payload renders as <div class=\"quote-card\">."""
    html = display_view_for(
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "exchange": "NMS",
            "currency": "USD",
            "price": 338.98,
            "change": 0.47,
            "change_pct": 1.16,
            "open": 339.0,
            "high": 342.0,
            "low": 337.5,
            "previous_close": 338.51,
            "volume": 49_000_000,
            "provider": "yfinance",
        },
        intent="quote",
    )
    assert isinstance(html, str)
    assert html.startswith('<div class="quote-card">'), html[:120]
    assert "AAPL" in html
    assert "Apple" in html


def test_intent_quote_error_renders_error_card():
    """Quote path with provider error must downgrade to error-card."""
    html = display_view_for(
        {"symbol": "AAPL", "error": "rate_limited", "error_code": "rate_limited"},
        intent="quote",
    )
    assert html.startswith('<div class="error-card">')
    assert "AAPL" in html
    assert "请求" in html


def test_intent_fundamentals_success():
    html = display_view_for(
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "pe_ratio": 38.92,
            "pb_ratio": 46.06,
            "market_cap": 4_947_135_430_656,
            "roe": 1.49,
            "eps": 8.71,
            "currency": "USD",
        },
        intent="fundamentals",
    )
    assert html.startswith('<div class="fundamentals-card">')
    assert "Apple" in html


def test_intent_fundamentals_null_field_handled():
    """Null / missing fields must not raise — fall back to '—' placeholder."""
    html = display_view_for(
        {"symbol": "AAPL", "pe_ratio": None, "pb_ratio": None, "market_cap": None, "roe": None},
        intent="fundamentals",
    )
    assert html.startswith('<div class="fundamentals-card">')
    # Empty/null fundamentals → friendly "暂无基本面数据" placeholder
    assert "暂无基本面数据" in html


def test_intent_history_with_candles_renders_history_card():
    html = display_view_for(
        {
            "symbol": "AAPL",
            "interval": "1d",
            "candles": [
                {"timestamp": "2026-09-20", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1_000_000},
                {"timestamp": "2026-09-21", "open": 101.0, "high": 103.5, "low": 100.5, "close": 102.5, "volume": 1_100_000},
                {"timestamp": "2026-09-22", "open": 102.5, "high": 104.0, "low": 102.0, "close": 103.7, "volume": 1_300_000},
            ],
        },
        intent="history",
    )
    assert html.startswith('<div class="history-card">')
    assert "AAPL" in html


def test_intent_history_empty_partial_data_renders_empty_card():
    """§0.4.26 — empty candles → friendly empty card with hint, not error."""
    html = display_view_for(
        {"symbol": "NEW.SS", "interval": "1d", "candles": [], "provider": "yfinance"},
        intent="history",
    )
    assert html.startswith('<div class="history-card empty">')
    assert "暂无历史数据" in html
    assert "数据源" in html


def test_intent_history_error_renders_error_card():
    html = display_view_for(
        {"symbol": "AAPL", "error": "no_data", "error_code": "no_data"},
        intent="history",
    )
    assert html.startswith('<div class="error-card">')
    assert "暂无数据" in html


def test_intent_news_with_items():
    html = display_view_for(
        {
            "symbol": "AAPL",
            "items": [
                {"title": "Apple beats earnings", "published_at": "2026-09-22", "url": "https://example.com/1"},
                {"title": "Apple unveils Vision Pro 2", "published_at": "2026-09-21", "url": "https://example.com/2"},
            ],
        },
        intent="news",
    )
    assert html.startswith('<div class="news-card">')
    assert "Apple beats earnings" in html
    assert "2 条" in html


def test_intent_news_empty():
    html = display_view_for(
        {"symbol": "AAPL", "items": []},
        intent="news",
    )
    assert html.startswith('<div class="news-card">')
    assert "暂无新闻" in html


def test_intent_alpha_with_factors():
    html = display_view_for(
        {"symbol": "AAPL", "factors": ["momentum_20", "rsi_14", "volatility_30"]},
        intent="alpha",
    )
    assert html.startswith('<div class="alpha-card">')
    assert "momentum_20" in html
    assert "3 个因子" in html


def test_intent_alpha_empty():
    html = display_view_for(
        {"symbol": "AAPL", "factors": []},
        intent="alpha",
    )
    assert html.startswith('<div class="alpha-card">')
    assert "暂无因子" in html


def test_intent_ack_note_created():
    html = display_view_for(
        {"status": "ok", "raw": "NOTE_CREATED: note-abc123", "id": "note-abc123"},
        intent="ack",
    )
    assert html.startswith('<div class="ack-card">')
    assert "笔记已创建" in html
    assert "note-abc123" in html


def test_intent_ack_alert_deleted():
    html = display_view_for(
        {"status": "ok", "raw": "ALERT_DELETED: alert-xyz789", "id": "alert-xyz789"},
        intent="ack",
    )
    assert html.startswith('<div class="ack-card">')
    assert "告警已删除" in html


def test_intent_error_no_data():
    html = display_view_for(
        {"error": "no_data", "error_code": "no_data", "symbol": "AAPL"},
        intent="error",
    )
    assert html.startswith('<div class="error-card">')
    assert "暂无数据" in html


def test_intent_error_unknown_code_falls_back_to_message():
    """Unknown error_code → use raw error string in the card body."""
    html = display_view_for(
        {"error": "Weird proprietary error", "error_code": "weird_code", "symbol": "AAPL"},
        intent="error",
    )
    assert html.startswith('<div class="error-card">')
    assert "Weird proprietary error" in html


def test_intent_error_no_symbol_still_renders():
    """Some error payloads don't carry a symbol — must not raise."""
    html = display_view_for(
        {"error": "internal", "error_code": "internal"},
        intent="error",
    )
    assert html.startswith('<div class="error-card">')


# ─────────────────────────────────────────────────────────────────
# Plain-text intents — returns prose, not wrapped in <div class=...
# ─────────────────────────────────────────────────────────────────

def test_intent_list_returns_plain_text_with_count():
    html = display_view_for(
        {"count": 3, "preview": "| id | symbol |\n| a | 1 |", "summary": "summary-fallback"},
        intent="list",
    )
    assert isinstance(html, str)
    assert '<div class=' not in html
    assert "共 3 条" in html
    # preview wins over summary in the list renderer
    assert "| a | 1 |" in html


def test_intent_list_empty_with_zero_count_returns_count_head():
    html = display_view_for(
        {"count": 0, "preview": "", "summary": ""},
        intent="list",
    )
    # count=0 still produces a count-prefixed string, no card wrapper
    assert "<div" not in html
    assert "共 0 条" in html


def test_intent_list_missing_count_and_text_returns_placeholder():
    html = display_view_for(
        {"preview": "", "summary": ""},  # no count, no text
        intent="list",
    )
    assert "<div" not in html
    assert "(空)" in html


def test_intent_report_read_parses_meta_and_content():
    html = display_view_for(
        {
            "text": (
                "REPORT: run-abc\n"
                "---meta---\n"
                '{"ticker":"AAPL","signal":"Overweight","status":"completed"}\n'
                "---content---\n"
                "苹果 2026 Q3 财报点评..."
            ),
        },
        intent="report_read",
    )
    assert isinstance(html, str)
    assert "run-abc" in html
    assert "Overweight" in html


# ─────────────────────────────────────────────────────────────────
# Defensive cases
# ─────────────────────────────────────────────────────────────────

def test_intent_unknown_falls_back_to_json():
    """Unknown intent → JSON dump, not crash."""
    out = display_view_for({"foo": "bar"}, intent="definitely_not_a_real_intent")
    assert isinstance(out, str)
    assert '"foo"' in out


def test_non_dict_input_serialised_to_json():
    """Non-dict payload → JSON, not crash."""
    out = display_view_for([1, 2, 3], intent="quote")
    assert isinstance(out, str)
    assert "1" in out and "3" in out


def test_frontend_regex_covers_every_card_class():
    """The frontend regex in harness.js (multi-intent bubble) accepts
    every card class we emit. If we add a new card class here that
    isn\'t in the regex, the bubble will drop it. This test pins
    the contract.
    """
    harness_js = open("web/static/harness.js").read()
    # The regex on disk is the literal class list inside ``/^<div\s+class="(...)"-card"/i``
    # (the backslash before ``s`` is part of the JS regex source itself).
    matches = re.findall(r'<div\\s\+class="\(([\w|]+)\)-card"', harness_js)
    assert matches, "frontend regex for multi-intent card class not found in harness.js"
    allowed: set[str] = set()
    for grp in matches:
        allowed.update(grp.split("|"))
    # Every card class we emit via display_view_for must be in the regex.
    for cls in ("quote", "fundamentals", "history", "news", "alpha", "ack", "error"):
        assert cls in allowed, f"frontend regex missing card class {cls!r}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
