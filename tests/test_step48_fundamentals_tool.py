"""§0.4.15 — get_fundamentals tool surfaces real provider data.

Regression for the "看一下 AAPL 的基本面数据" report: the tool used
to return every numeric field as ``None`` because it called
``get_identity`` (which only carries name/exchange/currency) and
then read pe_ratio / market_cap via ``getattr(..., None)`` against
an object that never had those attributes.

The fix:

1. New :class:`web.market_models.FundamentalsSnapshot` model carries
   PE / PB / market_cap / circulating_cap / ROE / revenue /
   net_income / EPS / dividend yield / 52w range.
2. :class:`tradingagents.data.providers.base.Provider` gets a default
   ``get_fundamentals`` that merges quote-side fundamentals (akshare +
   eastmoney fill these on QuoteSnapshot for A-shares) with identity
   metadata. When neither layer yields useful data it raises NO_DATA so
   :class:`ProviderFailover` walks to the next provider.
3. :class:`YFinanceProvider` overrides ``get_fundamentals`` to read
   ``ticker.info`` for US ticker fundamentals (PE / PB / ROE / EPS /
   52w high-low / dividend yield).
4. :class:`ProviderFailover` treats NO_DATA on ``get_fundamentals``
   as transient — eastmoney raising NO_DATA for AAPL should let
   yfinance answer instead of short-circuiting the chain.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.tools.builtin import (  # noqa: E402
    FundamentalsArgs,
    FundamentalsResult,
    get_fundamentals,
)


def test_fundamentals_snapshot_model_has_required_fields():
    from web.market_models import FundamentalsSnapshot
    fields = set(FundamentalsSnapshot.model_fields.keys())
    expected = {
        "symbol", "asset_type", "name", "exchange", "currency",
        "market_cap", "circulating_cap", "pe_ratio", "pb_ratio",
        "roe", "revenue", "net_income", "eps", "dividend_yield",
        "fifty_two_week_high", "fifty_two_week_low",
        "source", "as_of", "payload",
    }
    missing = expected - fields
    assert not missing, f"FundamentalsSnapshot missing fields: {missing}"


def test_provider_base_has_default_get_fundamentals():
    """Every provider must inherit a default impl."""
    from tradingagents.data.providers.base import Provider
    assert hasattr(Provider, "get_fundamentals"), (
        "Provider ABC must declare get_fundamentals so callers can rely "
        "on the contract"
    )


def test_yfinance_provider_implements_get_fundamentals():
    from tradingagents.data.providers.yfinance_provider import YFinanceProvider
    # Override (not just inheriting the default)
    assert "get_fundamentals" in YFinanceProvider.__dict__, (
        "YFinanceProvider must override get_fundamentals to surface "
        "ticker.info fields (PE/PB/ROE/EPS/52w range) the default "
        "merge-from-quote impl cannot reach"
    )


def test_default_get_fundamentals_raises_no_data_when_empty():
    """If neither get_quote nor get_identity produces useful data, the
    default impl must raise NO_DATA so the failover walks on.

    We exercise the path with an EastMoneyProvider call for a
    non-A-share symbol — eastmoney raises on get_quote AND
    get_identity for NVDA, and the default impl must NOT short-
    circuit with an all-None snapshot.
    """
    from tradingagents.data.providers.eastmoney_provider import EastMoneyProvider
    from web.market_models import ProviderError, ProviderErrorCode

    em = EastMoneyProvider()
    raised = False
    try:
        em.get_fundamentals("NVDA", "stock")
    except ProviderError as e:
        if e.code == ProviderErrorCode.NO_DATA:
            raised = True
    assert raised, (
        "EastMoneyProvider.get_fundamentals('NVDA') should raise "
        "NO_DATA so failover walks to yfinance — otherwise the "
        "failover declares eastmoney the winner with an all-None "
        "snapshot and yfinance never gets a turn"
    )


def test_provider_failover_walks_on_no_data_for_get_fundamentals():
    """E2E: NVDA via the failover. Eastmoney raises NO_DATA,
    failover walks to yfinance, yfinance returns the snapshot.
    """

    res = asyncio.run(_harness_call("NVDA"))
    assert res is not None
    assert res.symbol == "NVDA"
    # Either name should be populated, or some numeric field, or
    # the provider should be yfinance (eastmoney would have raised).
    has_anything = bool(
        res.name or res.market_cap or res.pe_ratio or res.pb_ratio
    )
    assert has_anything, (
        f"NVDA get_fundamentals came back empty: {res!r}"
    )
    # YFinance is the only US-ticker fundamentals source here; if
    # eastmoney had answered we would have a None-only snapshot.


def test_harness_get_fundamentals_returns_real_pe_ratio_for_aapl():
    """The §0.4.10-era regression: 'AAPL PE ratio' came back as
    ``None``. This must NOT regress."""

    res = asyncio.run(_harness_call("AAPL"))
    assert res is not None
    assert res.symbol == "AAPL"
    # AAPL's PE is around 30-40 (per yfinance ticker.info).
    assert res.pe_ratio is not None and res.pe_ratio > 5, (
        f"AAPL pe_ratio should be populated by yfinance; got {res.pe_ratio}"
    )
    assert res.market_cap is not None and res.market_cap > 1e10, (
        f"AAPL market_cap should be populated (multi-trillion); got {res.market_cap}"
    )
    # Provider must be yfinance for US tickers (eastmoney doesn't
    # know AAPL — failover walked past it).
    assert res.provider in ("yfinance",), (
        f"AAPL should resolve via yfinance; got provider={res.provider}"
    )


def test_harness_get_fundamentals_returns_real_pe_for_a_share():
    """A-share: 600036.SS should also have real PE / market cap
    (routed via yfinance since eastmoney's quote snapshot didn't
    carry fundamentals for this code on the live run).
    """
    from tradingagents.agent_harness.tools.builtin import (
        get_fundamentals, FundamentalsArgs,
    )

    res = asyncio.run(_harness_call("600036.SS"))
    assert res is not None
    assert res.symbol == "600036.SS"
    # Name may be English ("China Merchants Bank Co., Ltd.") since
    # yfinance's ticker.info carries the issuer's registered name,
    # not the Chinese-language display name. Just check the name is
    # populated and mentions "China Merchants" or "招商".
    assert res.name and ("招商" in res.name or "China Merchants" in res.name), (
        f"A-share name should be populated; got {res.name}"
    )
    assert res.pe_ratio is not None and 0 < res.pe_ratio < 100, (
        f"600036.SS PE should be populated (bank stock ~5-15); got {res.pe_ratio}"
    )


# ---- async helpers ---------------------------------------------------------

async def _harness_call(symbol):
    return await get_fundamentals(
        FundamentalsArgs(symbol=symbol, asset_type="stock"),
    )


if __name__ == "__main__":
    test_fundamentals_snapshot_model_has_required_fields()
    test_provider_base_has_default_get_fundamentals()
    test_yfinance_provider_implements_get_fundamentals()
    test_default_get_fundamentals_raises_no_data_when_empty()
    test_provider_failover_walks_on_no_data_for_get_fundamentals()
    test_harness_get_fundamentals_returns_real_pe_ratio_for_aapl()
    test_harness_get_fundamentals_returns_real_pe_for_a_share()
    print("ALL §0.4.15 fundamentals tests passed")
