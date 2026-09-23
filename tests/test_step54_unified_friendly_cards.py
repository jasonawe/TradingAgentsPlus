"""§0.4.22 — unified friendly cards (quote / fundamentals / news / alpha / ack)
and §0.4.23 — compare intent Tier 1 short-circuit."""
import pytest

from tradingagents.agent_harness.renderers.friendly_cards import (
    render_quote_card, render_fundamentals_card,
    render_news_card, render_alpha_card, render_ack_card,
)
from tradingagents.agent_harness.renderers.compare_sparkline import render_compare_card


# ── Quote card ───────────────────────────────────────────────

def test_quote_card_basic_shape():
    r = {"symbol": "AAPL", "name": "Apple", "exchange": "NMS",
         "currency": "USD", "price": 338.98, "change": 0.47,
         "change_pct": 1.16, "volume": 53197231, "provider": "yfinance"}
    html = render_quote_card(r)
    assert html.startswith('<div class="quote-card">')
    assert "AAPL" in html and "Apple" in html and "NMS" in html
    assert "qc-metrics" in html
    assert "338.98" in html


def test_quote_card_handles_missing_change():
    r = {"symbol": "X", "price": 100.0}
    html = render_quote_card(r)
    assert "100.00" in html
    # missing fields render as "—"
    assert "—" in html


def test_quote_card_color_class_for_direction():
    up = render_quote_card({"symbol": "X", "price": 100, "change": 1, "change_pct": 1})
    down = render_quote_card({"symbol": "X", "price": 100, "change": -1, "change_pct": -1})
    flat = render_quote_card({"symbol": "X", "price": 100, "change": 0, "change_pct": 0})
    assert 'class="m-v up"' in up
    assert 'class="m-v down"' in down
    assert 'class="m-v flat"' in flat


# ── Fundamentals card ────────────────────────────────────────

def test_fundamentals_card_renders_known_fields():
    r = {"symbol": "AAPL", "name": "Apple Inc.", "currency": "USD",
         "pe_ratio": 38.92, "pb_ratio": 46.06, "market_cap": 4.94e12,
         "roe": 1.49, "eps": 8.71, "provider": "yfinance"}
    html = render_fundamentals_card(r)
    assert html.startswith('<div class="fundamentals-card">')
    assert "AAPL" in html and "Apple Inc." in html
    assert "38.92" in html  # PE
    assert "46.06" in html  # PB
    # market_cap > 1e8 → "亿"
    assert "亿" in html or "4.94" in html


def test_fundamentals_card_empty():
    html = render_fundamentals_card({"symbol": "X"})
    assert "暂无基本面数据" in html


# ── News card ───────────────────────────────────────────────

def test_news_card_with_items():
    r = {"symbol": "AAPL", "items": [
        {"title": "News 1", "published_at": "2026-09-22"},
        {"title": "News 2", "url": "https://example.com/2"},
    ]}
    html = render_news_card(r)
    assert html.startswith('<div class="news-card">')
    assert "AAPL" in html
    assert "News 1" in html and "News 2" in html


def test_news_card_empty():
    html = render_news_card({"symbol": "X"})
    assert "暂无新闻" in html


# ── Alpha card ──────────────────────────────────────────────

def test_alpha_card_renders_chips():
    r = {"symbol": "600036.SS", "factors": ["alpha001", "alpha002", "alpha003"]}
    html = render_alpha_card(r)
    assert html.startswith('<div class="alpha-card">')
    assert "alpha001" in html
    assert "alpha002" in html


def test_alpha_card_empty():
    html = render_alpha_card({"symbol": "X"})
    assert "暂无因子" in html


# ── Ack card ────────────────────────────────────────────────

def test_ack_card_for_note_created():
    r = {"status": "ok", "raw": "NOTE_CREATED: {id: n1}", "id": "n1"}
    html = render_ack_card(r)
    assert html.startswith('<div class="ack-card">')
    assert "笔记已创建" in html
    assert "n1" in html


def test_ack_card_for_added():
    r = {"status": "ok", "raw": "ADDED: 600036.SS"}
    html = render_ack_card(r)
    assert "已加入" in html


# ── Compare card (already in step52, plus intent mapping) ───

def test_compare_card_works_with_2_series():
    series = [
        {"symbol": "AAPL", "name": "Apple", "closes": [100, 102, 104]},
        {"symbol": "NVDA", "name": "Nvidia", "closes": [50, 55, 60]},
    ]
    html = render_compare_card(series)
    assert html.startswith('<div class="compare-card">')
    assert "AAPL" in html and "NVDA" in html
