"""Alpha provider registry + active-provider selector (W3-D2 E3).

Mirrors :mod:`tradingagents.data.providers.registry` (quote seam, E1)
and :mod:`tradingagents.data.providers.news_registry` (news seam, E2)
so the settings UI / harness tool can resolve any of the three seams
through the same import pattern.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from .alpha_akshare_provider import AKShareAlphaProvider
from .alpha_base import AlphaProvider
from .alpha_stub_provider import StubAlphaProvider
from .alpha_yfinance_provider import YFinanceAlphaProvider

ALPHA_PROVIDERS: dict[str, AlphaProvider] = {
    "stub": StubAlphaProvider(),
    "yfinance": YFinanceAlphaProvider(),
    "akshare": AKShareAlphaProvider(),
}

_active_lock = threading.Lock()
_active: str = os.environ.get("TRADINGAGENTS_ALPHA_PROVIDER", "yfinance")


def get_active_alpha_provider_name() -> str:
    return _active


def set_active_alpha_provider(name: str) -> None:
    """Set the active alpha provider. Raises ``KeyError`` if ``name`` is unknown."""
    global _active
    with _active_lock:
        if name not in ALPHA_PROVIDERS:
            raise KeyError(
                f"unknown alpha provider {name!r}; known: {sorted(ALPHA_PROVIDERS)}"
            )
        _active = name


def get_active_alpha_provider() -> AlphaProvider:
    with _active_lock:
        name = _active
    return ALPHA_PROVIDERS[name]


def get_alpha_provider(name: str) -> AlphaProvider:
    if name not in ALPHA_PROVIDERS:
        raise KeyError(
            f"unknown alpha provider {name!r}; known: {sorted(ALPHA_PROVIDERS)}"
        )
    return ALPHA_PROVIDERS[name]


def select_alpha_provider(
    symbol: str, asset_type: str, *, preferred: Optional[str] = None,
) -> AlphaProvider:
    """Pick a provider that supports ``(symbol, asset_type)``."""
    candidates = []
    if preferred and preferred in ALPHA_PROVIDERS:
        candidates.append(ALPHA_PROVIDERS[preferred])
    candidates.append(get_active_alpha_provider())
    for name, provider in ALPHA_PROVIDERS.items():
        if provider in candidates:
            continue
        candidates.append(provider)

    for provider in candidates:
        if provider.supports(symbol, asset_type):
            return provider
    return get_active_alpha_provider()
