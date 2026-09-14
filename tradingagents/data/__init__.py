"""Unified data layer for TradingAgentsPlus (v3 P2).

Exposes the Provider ABC, DataResponse container, SQLite-backed cache,
and the active-provider registry that lets callers swap market data
backends at runtime without touching call sites.
"""
from .responses import DataResponse
from .cache import ProviderCache, get_default_cache
from .providers.base import Provider
from .providers.registry import (
    PROVIDERS,
    get_active_provider,
    get_active_provider_name,
    set_active_provider,
)

__all__ = [
    "DataResponse",
    "ProviderCache",
    "Provider",
    "PROVIDERS",
    "get_active_provider",
    "get_active_provider_name",
    "set_active_provider",
    "get_default_cache",
]
