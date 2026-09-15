"""W3-D2 E2: news_provider seam."""
from __future__ import annotations

import asyncio

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.data.providers import (  # noqa: E402
    AlphaVantageNewsProvider,
    NewsArticle,
    NewsWindow,
    StubNewsProvider,
    YFinanceNewsProvider,
)
from tradingagents.data.providers.news_registry import (  # noqa: E402
    NEWS_PROVIDERS,
    get_active_news_provider,
    get_active_news_provider_name,
    get_news_provider,
    select_news_provider,
    set_active_news_provider,
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_news_window_stub_classmethod() -> None:
    win = NewsWindow.stub("AAPL")
    assert win.symbol == "AAPL"
    assert len(win.items) == 1
    assert win.items[0].title == "[stub] news for AAPL"
    assert win.provider == "stub"


def test_news_window_empty_is_distinct_from_stub() -> None:
    """Real providers return empty NewsWindow when upstream has no news;
    consumers must be able to tell the difference from a stub placeholder.
    """
    win = NewsWindow(symbol="AAPL", items=[], provider="yfinance")
    assert win.items == []
    assert win.provider != "stub"


# ---------------------------------------------------------------------------
# ABC + concrete providers
# ---------------------------------------------------------------------------


def test_stub_supports_everything() -> None:
    p = StubNewsProvider()
    assert p.name == "stub"
    assert p.supports("AAPL", "stock")
    assert p.supports("BTCUSD", "crypto")
    assert p.supports("513880.SS", "fund")


def test_stub_returns_placeholder_window() -> None:
    win = StubNewsProvider().get_news("AAPL", days=7)
    assert win.symbol == "AAPL"
    assert len(win.items) == 1
    assert win.items[0].title.startswith("[stub]")


def test_yfinance_supports_common_assets() -> None:
    p = YFinanceNewsProvider()
    assert p.name == "yfinance"
    assert p.supports("AAPL", "stock")
    assert p.supports("BTCUSD", "crypto")


def test_yfinance_empty_when_no_news() -> None:
    """When upstream returns \"no news found\" we must return empty window
    (not a stub) so the LLM can tell the difference.
    """
    p = YFinanceNewsProvider()

    # Monkey-patch the dataflow import to return "no news" without
    # touching the network.
    def _stub_no_news(ticker, start_date, end_date):
        return f"No news found for {ticker} between {start_date} and {end_date}"

    import tradingagents.dataflows.yfinance_news as mod
    original = mod.get_news_yfinance
    mod.get_news_yfinance = _stub_no_news
    try:
        win = p.get_news("AAPL", days=7)
    finally:
        mod.get_news_yfinance = original

    assert win.symbol == "AAPL"
    assert win.items == []
    assert win.provider == "yfinance"


def test_yfinance_parses_markdown_articles() -> None:
    p = YFinanceNewsProvider()
    md = (
        "## AAPL News, from 2026-01-01 to 2026-01-07:\n\n"
        "### Apple beats estimates (source: Reuters)\n"
        "summary text\n\n"
        "### New product launch (source: Bloomberg)\n"
    )
    items = p.__class__.__module__  # noqa: F841 - just to keep ref
    from tradingagents.data.providers.news_yfinance_provider import (
        _parse_yfinance_markdown,
    )
    parsed = _parse_yfinance_markdown(md, "AAPL")
    assert len(parsed) == 2
    assert parsed[0].title == "Apple beats estimates"
    assert parsed[0].source == "Reuters"
    assert parsed[1].source == "Bloomberg"


def test_alpha_vantage_supports_only_stocks() -> None:
    p = AlphaVantageNewsProvider()
    assert p.supports("AAPL", "stock")
    assert not p.supports("BTCUSD", "crypto")
    assert not p.supports("513880.SS", "fund")


def test_alpha_vantage_empty_when_no_news() -> None:
    p = AlphaVantageNewsProvider()

    def _stub_empty(ticker, start_date, end_date):
        return {}

    import tradingagents.dataflows.alpha_vantage_news as mod
    original = mod.get_news
    mod.get_news = _stub_empty
    try:
        win = p.get_news("AAPL", days=7)
    finally:
        mod.get_news = original

    assert win.items == []
    assert win.provider == "alpha_vantage"


def test_alpha_vantage_parses_dict() -> None:
    from tradingagents.data.providers.news_alpha_vantage_provider import (
        _parse_alpha_vantage,
    )
    raw = {"Title A": "Summary A", "Title B": ""}
    parsed = _parse_alpha_vantage(raw)
    assert len(parsed) == 2
    assert parsed[0].title == "Title A"
    assert parsed[0].summary == "Summary A"
    assert parsed[1].summary is None  # empty string becomes None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_three_providers() -> None:
    assert set(NEWS_PROVIDERS) == {"stub", "yfinance", "alpha_vantage"}


def test_get_news_provider_known() -> None:
    p = get_news_provider("stub")
    assert isinstance(p, StubNewsProvider)


def test_get_news_provider_unknown_raises() -> None:
    with pytest.raises(KeyError, match="unknown news provider"):
        get_news_provider("nope")


def test_set_and_get_active() -> None:
    original = get_active_news_provider_name()
    try:
        set_active_news_provider("yfinance")
        assert get_active_news_provider_name() == "yfinance"
        assert get_active_news_provider().name == "yfinance"
    finally:
        set_active_news_provider(original)


def test_set_active_unknown_raises() -> None:
    with pytest.raises(KeyError):
        set_active_news_provider("nope")


def test_select_news_provider_preferred_wins() -> None:
    p = select_news_provider("AAPL", "stock", preferred="stub")
    assert p.name == "stub"


def test_select_news_provider_falls_back() -> None:
    """If the active provider doesn't support, pick the first that does."""
    original = get_active_news_provider_name()
    try:
        # alpha_vantage only supports stock; fund should fall back.
        set_active_news_provider("alpha_vantage")
        p = select_news_provider("513880.SS", "fund")
        # stub supports everything → should win
        assert p.name in {"stub", "yfinance"}
    finally:
        set_active_news_provider(original)


# ---------------------------------------------------------------------------
# Tool integration
# ---------------------------------------------------------------------------


def test_get_news_tool_uses_active_provider(monkeypatch) -> None:
    """The harness tool should call the active provider, not the hard-coded stub."""
    from tradingagents.agent_harness.tools.builtin import NewsArgs, get_news
    from tradingagents.data.providers import news_registry

    original = news_registry.get_active_news_provider_name()
    captured: list[str] = []

    class _CaptureProvider:
        name = "capture"

        def supports(self, symbol, asset_type): return True

        def get_news(self, symbol, *, days=7, asset_type="stock"):
            captured.append(symbol)
            return NewsWindow.stub(symbol)

    news_registry.NEWS_PROVIDERS["capture"] = _CaptureProvider()
    try:
        news_registry.set_active_news_provider("capture")
        result = asyncio.run(get_news(NewsArgs(symbol="TSLA", days=5)))
        assert captured == ["TSLA"]
        assert result.symbol == "TSLA"
    finally:
        news_registry.NEWS_PROVIDERS.pop("capture", None)
        news_registry.set_active_news_provider(original)
