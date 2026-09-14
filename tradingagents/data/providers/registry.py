"""Active-provider registry (v3 P2 §9).

A single process-wide ``active_provider`` name is the default used by
callers that don't pass a provider explicitly. Callers can override it
via :func:`set_active_provider` (settings UI) or pass ``provider=`` to
the route layer to override per-request.

The ``"auto"`` sentinel picks the first provider that ``supports()``
the requested ``(symbol, asset_type, capability)`` triple, falling back
to the configured default if none do.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from .akshare_provider import AKShareProvider
from .alpha_vantage_provider import AlphaVantageProvider
from .base import Provider
from .eastmoney_provider import EastMoneyProvider
from .yfinance_provider import YFinanceProvider

PROVIDERS: dict[str, Provider] = {
    "yfinance": YFinanceProvider(),
    "eastmoney": EastMoneyProvider(),
    "akshare": AKShareProvider(),
    "alpha_vantage": AlphaVantageProvider(),
}

_active_lock = threading.Lock()
_active: str = os.environ.get("TRADINGAGENTS_DATA_PROVIDER", "eastmoney")


def get_active_provider_name() -> str:
    return _active


def set_active_provider(name: str) -> None:
    """Set the active provider. Raises ``KeyError`` if ``name`` is unknown."""
    global _active
    with _active_lock:
        if name not in PROVIDERS:
            raise KeyError(
                f"unknown provider {name!r}; known: {sorted(PROVIDERS)}"
            )
        _active = name


def get_active_provider() -> Provider:
    with _active_lock:
        name = _active
    return PROVIDERS[name]


def get_provider(name: str) -> Provider:
    if name not in PROVIDERS:
        raise KeyError(
            f"unknown provider {name!r}; known: {sorted(PROVIDERS)}"
        )
    return PROVIDERS[name]


def select_provider(
    symbol: str,
    asset_type: str,
    capability: str,
    preferred: Optional[str] = None,
) -> Provider:
    """Pick a provider that supports ``(symbol, asset_type, capability)``.

    Order: ``preferred`` → active provider → first registered provider
    that ``supports()`` the request. Falls back to the active provider
    if none advertise support (avoid breaking existing behaviour).
    """
    candidates = []
    if preferred and preferred in PROVIDERS:
        candidates.append(PROVIDERS[preferred])
    candidates.append(get_active_provider())
    for name, provider in PROVIDERS.items():
        if provider in candidates:
            continue
        candidates.append(provider)

    for provider in candidates:
        if provider.supports(symbol, asset_type, capability):
            return provider
    return get_active_provider()
