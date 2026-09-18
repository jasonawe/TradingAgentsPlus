"""Step 24 — D2 data-layer migration: 8 read tools carry metadata.
import pytest


Data fetch tools are tagged with their primary capability:

- get_quote / get_quotes_batch → quote
- get_history → history
- get_fundamentals → fundamentals
- get_news → news
- list_alpha_factors / compute_alpha_factors / evaluate_alpha → alpha

All eight carry category=data.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _reg():
    from tradingagents.agent_harness.tools import ToolRegistry
    from tradingagents.agent_harness.tools.builtin import install_builtin_tools
    reg = ToolRegistry()
    install_builtin_tools(reg)
    return reg


def _meta(name: str) -> dict:
    return _reg().get(name).schema.metadata





def test_get_quotes_batch_metadata():
    md = _meta("get_quotes_batch")
    assert md["capabilities"] == ["quote"]
    assert md["display_view"] == "quote"
    assert md["category"] == "data"


def test_get_history_metadata():
    md = _meta("get_history")
    assert md["capabilities"] == ["history"]
    assert md["display_view"] == "history"
    assert md["category"] == "data"


def test_get_fundamentals_metadata():
    md = _meta("get_fundamentals")
    assert md["capabilities"] == ["fundamentals"]
    assert md["display_view"] == "fundamentals"
    assert md["category"] == "data"


def test_get_news_metadata():
    md = _meta("get_news")
    assert md["capabilities"] == ["news"]
    assert md["display_view"] == "news"
    assert md["category"] == "data"


def test_alpha_tools_metadata():
    """The three alpha tools tag ALPHA + alpha_list."""
    for n in ("list_alpha_factors", "compute_alpha_factors", "evaluate_alpha"):
        md = _meta(n)
        assert md["capabilities"] == ["alpha"], n
        assert md["display_view"] == "alpha_list", n
        assert md["category"] == "data", n


def test_data_layer_count():
    """All 8 migrated data tools exist."""
    expected = {
        "get_quote", "get_quotes_batch", "get_history", "get_fundamentals",
        "get_news", "list_alpha_factors", "compute_alpha_factors",
        "evaluate_alpha",
    }
    for n in expected:
        md = _meta(n)
        assert md, f"{n} still has empty metadata"
        assert md["category"] == "data"


def test_display_view_helper_renders_history():
    """Confirm display_view helper actually renders history payloads."""
    from tradingagents.agent_harness.tools.display_view import display_view_for
    payload = {
        "symbol": "600036.SS",
        "interval": "1d",
        "candles": [
            {"close": 40.0, "timestamp": "2026-09-01"},
            {"close": 41.0, "timestamp": "2026-09-02"},
        ],
    }
    out = display_view_for(payload, intent="history")
    assert "600036.SS" in out
    assert "1d" in out
    assert "41.00" in out


def test_display_view_helper_renders_news():
    from tradingagents.agent_harness.tools.display_view import display_view_for
    payload = {
        "symbol": "NVDA",
        "items": [
            {"title": "Earnings beat", "url": "x", "published_at": "2026-09-16"},
            {"title": "AI demand strong", "url": "x", "published_at": "2026-09-16"},
        ],
    }
    out = display_view_for(payload, intent="news")
    assert "NVDA" in out
    assert "Earnings beat" in out


def test_display_view_helper_renders_fundamentals():
    from tradingagents.agent_harness.tools.display_view import display_view_for
    payload = {
        "symbol": "600036.SS",
        "pe_ratio": 5.5,
        "pb_ratio": 0.6,
        "market_cap": 1_032_500_000_000,
        "roe": 15.2,
    }
    out = display_view_for(payload, intent="fundamentals")
    assert "600036.SS" in out
    assert "PE" in out and "5.5" in out
    assert "PB" in out and "0.6" in out
