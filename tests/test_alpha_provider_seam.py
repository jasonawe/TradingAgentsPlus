"""W3-D2 E3: alpha_provider seam."""
from __future__ import annotations

import asyncio

import sys
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.data.providers import (  # noqa: E402
    AKShareAlphaProvider,
    AlphaProvider,
    StubAlphaProvider,
    YFinanceAlphaProvider,
)
from tradingagents.data.providers.alpha_registry import (  # noqa: E402
    ALPHA_PROVIDERS,
    get_active_alpha_provider,
    get_active_alpha_provider_name,
    get_alpha_provider,
    select_alpha_provider,
    set_active_alpha_provider,
)


# ---------------------------------------------------------------------------
# ABC + concrete providers
# ---------------------------------------------------------------------------


def test_stub_alpha_supports_everything() -> None:
    p = StubAlphaProvider()
    assert p.name == "stub"
    assert p.supports("AAPL", "stock")
    assert p.supports("BTCUSD", "crypto")


def test_stub_alpha_returns_empty_dataframe() -> None:
    df = StubAlphaProvider().load_ohlcv("AAPL")
    assert isinstance(df, pd.DataFrame)
    assert df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_yfinance_alpha_supports_common_assets() -> None:
    p = YFinanceAlphaProvider()
    assert p.name == "yfinance"
    assert p.supports("AAPL", "stock")
    assert p.supports("BTCUSD", "crypto")


def test_yfinance_alpha_returns_dataframe_on_failure() -> None:
    """When upstream fails the provider must return an empty OHLCV
    DataFrame (not raise) so the harness tool can fall back to zeros.
    """
    p = YFinanceAlphaProvider()
    # load_ohlcv swallows exceptions; with no cache for a fake symbol
    # the call will fail/return-empty, but never raise.
    df = p.load_ohlcv("DEFINITELY_NOT_A_REAL_TICKER_9999")
    assert isinstance(df, pd.DataFrame)
    # Either empty (failure path) or with the OHLCV columns (cached)
    if not df.empty:
        assert {"Open", "High", "Low", "Close"}.issubset(df.columns)


def test_akshare_alpha_only_supports_a_shares() -> None:
    p = AKShareAlphaProvider()
    assert p.name == "akshare"
    assert p.supports("600036.SS", "stock")
    assert p.supports("000001.SZ", "stock")
    assert not p.supports("AAPL", "stock")  # US, not A-share
    assert not p.supports("BTCUSD", "crypto")


def test_akshare_alpha_returns_empty_when_not_installed() -> None:
    """akshare is an optional dep; provider must not raise if missing."""
    p = AKShareAlphaProvider()
    df = p.load_ohlcv("600036.SS")
    assert isinstance(df, pd.DataFrame)
    # Either empty (akshare missing or fetch failed) or full bars.
    if not df.empty:
        assert {"Open", "High", "Low", "Close"}.issubset(df.columns)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_three_providers() -> None:
    assert set(ALPHA_PROVIDERS) == {"stub", "yfinance", "akshare"}


def test_get_alpha_provider_known() -> None:
    p = get_alpha_provider("stub")
    assert isinstance(p, StubAlphaProvider)


def test_get_alpha_provider_unknown_raises() -> None:
    with pytest.raises(KeyError, match="unknown alpha provider"):
        get_alpha_provider("nope")


def test_set_and_get_active() -> None:
    original = get_active_alpha_provider_name()
    try:
        set_active_alpha_provider("stub")
        assert get_active_alpha_provider_name() == "stub"
        assert get_active_alpha_provider().name == "stub"
    finally:
        set_active_alpha_provider(original)


def test_set_active_unknown_raises() -> None:
    with pytest.raises(KeyError):
        set_active_alpha_provider("nope")


def test_select_alpha_provider_preferred_wins() -> None:
    p = select_alpha_provider("AAPL", "stock", preferred="stub")
    assert p.name == "stub"


def test_select_alpha_provider_falls_back_for_non_a_share() -> None:
    """akshare supports only A-shares; for a US ticker it should
    fall back to yfinance (which supports US stocks) or stub.
    """
    original = get_active_alpha_provider_name()
    try:
        set_active_alpha_provider("akshare")
        p = select_alpha_provider("AAPL", "stock")
        assert p.name in {"yfinance", "stub"}
    finally:
        set_active_alpha_provider(original)


# ---------------------------------------------------------------------------
# Tool integration
# ---------------------------------------------------------------------------


def test_list_alpha_factors_uses_local_library() -> None:
    """list_alpha_factors must enumerate from the alpha158 library, not
    return the hard-coded ['alpha_001', 'alpha_002', 'alpha_003'] stub.
    """
    from tradingagents.agent_harness.tools.builtin import list_alpha_factors
    result = asyncio.run(list_alpha_factors())
    # The real library has 30+ factors; if it fell back to the placeholder
    # we'd see exactly 3 entries. We assert the library path was taken.
    assert len(result.factors) > 3
    # Should include some canonical factor names from the library.
    assert any("roc" in f.lower() or "rsi" in f.lower() for f in result.factors)


def test_compute_alpha_factors_falls_back_to_zeros_on_empty() -> None:
    from tradingagents.agent_harness.tools.builtin import (
        ComputeAlphaFactorsArgs, compute_alpha_factors,
    )
    from tradingagents.data.providers import alpha_registry
    original = alpha_registry.get_active_alpha_provider_name()
    try:
        alpha_registry.set_active_alpha_provider("stub")
        result = asyncio.run(compute_alpha_factors(
            ComputeAlphaFactorsArgs(symbol="AAPL", factors=["alpha_001", "alpha_002"]),
        ))
        assert result.symbol == "AAPL"
        assert result.values == {"alpha_001": 0.0, "alpha_002": 0.0}
    finally:
        alpha_registry.set_active_alpha_provider(original)


def test_evaluate_alpha_falls_back_to_zeros_on_empty() -> None:
    from tradingagents.agent_harness.tools.builtin import (
        EvaluateAlphaArgs, evaluate_alpha,
    )
    from tradingagents.data.providers import alpha_registry
    original = alpha_registry.get_active_alpha_provider_name()
    try:
        alpha_registry.set_active_alpha_provider("stub")
        result = asyncio.run(evaluate_alpha(
            EvaluateAlphaArgs(symbol="AAPL", factor="roc_5", horizon_days=5),
        ))
        assert result.symbol == "AAPL"
        assert result.factor == "roc_5"
        assert result.ic == 0.0
        assert result.rank_ic == 0.0
    finally:
        alpha_registry.set_active_alpha_provider(original)
