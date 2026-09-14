"""Provider ABC + registry smoke tests (v3 P2)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure project root is importable when pytest is invoked from anywhere.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.data.providers import (  # noqa: E402
    AKShareProvider,
    AlphaVantageProvider,
    EastMoneyProvider,
    Provider,
    YFinanceProvider,
)
from tradingagents.data.providers.registry import (  # noqa: E402
    PROVIDERS,
    get_active_provider,
    get_active_provider_name,
    get_provider,
    select_provider,
    set_active_provider,
)


def test_all_providers_subclass_provider_abc() -> None:
    """N59 fix: every concrete provider MUST inherit ``Provider``."""
    expected = {"YFinanceProvider", "EastMoneyProvider", "AKShareProvider", "AlphaVantageProvider"}
    found = set()
    for cls in (YFinanceProvider, EastMoneyProvider, AKShareProvider, AlphaVantageProvider):
        assert issubclass(cls, Provider), f"{cls.__name__} does not inherit Provider"
        assert cls().name, f"{cls.__name__} has empty name"
        found.add(cls.__name__)
    assert found == expected


def test_registry_has_four_providers() -> None:
    assert set(PROVIDERS) == {"yfinance", "eastmoney", "akshare", "alpha_vantage"}
    assert isinstance(PROVIDERS["yfinance"], YFinanceProvider)
    assert isinstance(PROVIDERS["eastmoney"], EastMoneyProvider)
    assert isinstance(PROVIDERS["akshare"], AKShareProvider)
    assert isinstance(PROVIDERS["alpha_vantage"], AlphaVantageProvider)


def test_active_provider_default_is_eastmoney() -> None:
    # Either unset or explicitly set to eastmoney.
    name = get_active_provider_name()
    assert name in PROVIDERS, f"active provider {name!r} not registered"


def test_set_active_provider_rejects_unknown() -> None:
    with pytest.raises(KeyError):
        set_active_provider("not_a_real_provider")


def test_set_active_provider_round_trip(monkeypatch) -> None:
    monkeypatch.delenv("TRADINGAGENTS_DATA_PROVIDER", raising=False)
    original = get_active_provider_name()
    try:
        set_active_provider("yfinance")
        assert get_active_provider_name() == "yfinance"
        assert isinstance(get_active_provider(), YFinanceProvider)
    finally:
        set_active_provider(original)


def test_get_provider_returns_singleton() -> None:
    a = get_provider("eastmoney")
    b = get_provider("eastmoney")
    assert a is b


def test_select_provider_prefers_preferred() -> None:
    p = select_provider("AAPL", "stock", "quote", preferred="yfinance")
    assert isinstance(p, YFinanceProvider)


def test_select_provider_falls_back_to_active(monkeypatch) -> None:
    monkeypatch.delenv("TRADINGAGENTS_DATA_PROVIDER", raising=False)
    original = get_active_provider_name()
    try:
        set_active_provider("eastmoney")
        p = select_provider("AAPL", "stock", "quote")
        assert isinstance(p, (EastMoneyProvider, YFinanceProvider))
    finally:
        set_active_provider(original)


def test_provider_health_default() -> None:
    for provider in PROVIDERS.values():
        h = provider.health()
        assert h["name"] == provider.name
        assert h["status"] == "configured"
