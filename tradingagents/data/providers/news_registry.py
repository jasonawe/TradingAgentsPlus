"""News provider registry + active-provider selector (W3-D2 E2).

Mirrors :mod:`tradingagents.data.providers.registry` for the quote
seam so the settings UI / harness tool can treat news the same way
they treat quote data.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from .news_alpha_vantage_provider import AlphaVantageNewsProvider
from .news_base import NewsProvider
from .news_stub_provider import StubNewsProvider
from .news_yfinance_provider import YFinanceNewsProvider

NEWS_PROVIDERS: dict[str, NewsProvider] = {
    "stub": StubNewsProvider(),
    "yfinance": YFinanceNewsProvider(),
    "alpha_vantage": AlphaVantageNewsProvider(),
}

_active_lock = threading.Lock()
_active: str = os.environ.get("TRADINGAGENTS_NEWS_PROVIDER", "stub")


def get_active_news_provider_name() -> str:
    return _active


def set_active_news_provider(name: str) -> None:
    """Set the active news provider. Raises ``KeyError`` if ``name`` is unknown."""
    global _active
    with _active_lock:
        if name not in NEWS_PROVIDERS:
            raise KeyError(
                f"unknown news provider {name!r}; known: {sorted(NEWS_PROVIDERS)}"
            )
        _active = name


def get_active_news_provider() -> NewsProvider:
    with _active_lock:
        name = _active
    return NEWS_PROVIDERS[name]


def get_news_provider(name: str) -> NewsProvider:
    if name not in NEWS_PROVIDERS:
        raise KeyError(
            f"unknown news provider {name!r}; known: {sorted(NEWS_PROVIDERS)}"
        )
    return NEWS_PROVIDERS[name]


def select_news_provider(
    symbol: str, asset_type: str, *, preferred: Optional[str] = None,
) -> NewsProvider:
    """Pick a provider that supports ``(symbol, asset_type)``.

    Order: ``preferred`` → active provider → first registered provider
    that ``supports()`` the request. Falls back to the active provider
    if none advertise support (preserves legacy behaviour).
    """
    candidates = []
    if preferred and preferred in NEWS_PROVIDERS:
        candidates.append(NEWS_PROVIDERS[preferred])
    candidates.append(get_active_news_provider())
    for name, provider in NEWS_PROVIDERS.items():
        if provider in candidates:
            continue
        candidates.append(provider)

    for provider in candidates:
        if provider.supports(symbol, asset_type):
            return provider
    return get_active_news_provider()
