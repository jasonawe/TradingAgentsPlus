"""W3-D2 E1 polish: backend PATCH endpoints for news/alpha providers."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.data.providers.news_registry import NEWS_PROVIDERS as NEWS_PROVIDERS_NAMES
from tradingagents.data.providers.alpha_registry import ALPHA_PROVIDERS as ALPHA_PROVIDERS_NAMES
from tradingagents.data.providers.news_registry import (
    get_active_news_provider_name, set_active_news_provider,
)
from tradingagents.data.providers.alpha_registry import (
    get_active_alpha_provider_name, set_active_alpha_provider,
)


def test_news_provider_options_match_registry() -> None:
    """The settings UI options must mirror NEWS_PROVIDERS keys."""
    assert set(NEWS_PROVIDERS_NAMES) >= {"stub", "yfinance", "alpha_vantage"}


def test_alpha_provider_options_match_registry() -> None:
    assert set(ALPHA_PROVIDERS_NAMES) >= {"stub", "yfinance", "akshare"}


def test_switch_news_then_alpha_does_not_cross_contaminate() -> None:
    """Switching news must not affect alpha and vice versa."""
    original_news = get_active_news_provider_name()
    original_alpha = get_active_alpha_provider_name()
    try:
        set_active_news_provider("yfinance")
        set_active_alpha_provider("stub")
        assert get_active_news_provider_name() == "yfinance"
        assert get_active_alpha_provider_name() == "stub"
    finally:
        set_active_news_provider(original_news)
        set_active_alpha_provider(original_alpha)


def test_unknown_news_provider_raises() -> None:
    with pytest.raises(KeyError, match="unknown news provider"):
        set_active_news_provider("nope_999")


def test_unknown_alpha_provider_raises() -> None:
    with pytest.raises(KeyError, match="unknown alpha provider"):
        set_active_alpha_provider("nope_999")
