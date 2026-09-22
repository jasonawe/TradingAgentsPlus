from __future__ import annotations

from datetime import datetime, timezone

import yfinance as yf

from cli.utils import is_valid_ticker_input, normalize_ticker_symbol

from .base import Provider

from web.market_models import (
    AssetIdentity,
    Candle,
    FundamentalsSnapshot,
    Freshness,
    ProviderError,
    ProviderErrorCode,
    QuoteSnapshot,
)


class YFinanceProvider(Provider):
    name = "yfinance"

    def supports(self, symbol: str, asset_type: str, capability: str) -> bool:
        return asset_type in {"stock", "crypto"} and capability in {"quote", "candles", "identity"}

    def _ticker(self, symbol: str):
        if not is_valid_ticker_input(symbol):
            raise ProviderError(ProviderErrorCode.INVALID_SYMBOL, "invalid symbol")
        return yf.Ticker(normalize_ticker_symbol(symbol))

    def get_quote(self, symbol: str, asset_type: str) -> QuoteSnapshot:
        ticker = self._ticker(symbol)
        info = _info(ticker)
        try:
            frame = ticker.history(period="5d", interval="1d")
        except TimeoutError as exc:
            raise ProviderError(ProviderErrorCode.TIMEOUT, str(exc)) from exc
        except Exception as exc:
            raise ProviderError(ProviderErrorCode.PROVIDER_ERROR, str(exc)) from exc
        if frame is None or frame.empty or "Close" not in frame:
            raise ProviderError(ProviderErrorCode.NO_DATA, "no quote data")
        close = frame["Close"].dropna()
        if close.empty:
            raise ProviderError(ProviderErrorCode.NO_DATA, "no quote data")
        price = float(close.iloc[-1])
        previous = float(close.iloc[-2]) if len(close) > 1 else None
        as_of = frame.index[-1]
        if hasattr(as_of, "to_pydatetime"):
            as_of = as_of.to_pydatetime()
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        change = price - previous if previous is not None else None
        return QuoteSnapshot(
            symbol=normalize_ticker_symbol(symbol),
            asset_type=asset_type,
            price=price,
            previous_close=previous,
            change=change,
            change_percent=(change / previous * 100 if change is not None and previous else None),
            open=float(frame["Open"].iloc[-1]) if "Open" in frame else None,
            high=float(frame["High"].iloc[-1]) if "High" in frame else None,
            low=float(frame["Low"].iloc[-1]) if "Low" in frame else None,
            volume=float(frame["Volume"].iloc[-1]) if "Volume" in frame else None,
            currency=info.get("currency"),
            as_of=as_of,
            fetched_at=datetime.now(timezone.utc),
            freshness=Freshness.DELAYED,
            is_delayed=True,
            source=self.name,
            exchange=info.get("exchange"),
            raw_summary=info.get("longName") or info.get("shortName"),
        )

    def get_candles(self, symbol: str, interval: str, start, end) -> list[Candle]:
        ticker = self._ticker(symbol)
        try:
            frame = ticker.history(start=start, end=end, interval=interval)
        except Exception as exc:
            raise ProviderError(ProviderErrorCode.PROVIDER_ERROR, str(exc)) from exc
        if frame is None or frame.empty:
            raise ProviderError(ProviderErrorCode.NO_DATA, "no candle data")
        result = []
        for ts, row in frame.iterrows():
            result.append(
                Candle(
                    symbol=normalize_ticker_symbol(symbol),
                    interval=interval,
                    timestamp=ts,
                    open=row.get("Open"),
                    high=row.get("High"),
                    low=row.get("Low"),
                    close=row.get("Close"),
                    volume=row.get("Volume"),
                    source=self.name,
                )
            )
        return result

    def get_identity(self, symbol: str, asset_type: str) -> AssetIdentity:
        ticker = self._ticker(symbol)
        try:
            info = ticker.info or {}
        except Exception as exc:
            raise ProviderError(ProviderErrorCode.PROVIDER_ERROR, str(exc)) from exc
        return AssetIdentity(
            symbol=normalize_ticker_symbol(symbol),
            asset_type=asset_type,
            name=info.get("longName") or info.get("shortName"),
            exchange=info.get("exchange"),
            currency=info.get("currency"),
        )

    def get_fundamentals(self, symbol: str, asset_type: str = "stock") -> FundamentalsSnapshot:
        """§0.4.15 — yfinance fundamentals from ``ticker.info``.

        YFinance's :meth:`get_quote` only carries price + OHLCV; the
        rich fundamentals (PE / PB / ROE / EPS / market cap /
        52-week high-low / dividend yield) live on ``ticker.info``.
        We pull those here so :func:`tradingagents.agent_harness.tools.
        builtin.get_fundamentals` has real data for US tickers, not
        the all-``None`` fallback the previous ``get_identity``-based
        impl produced.
        """
        ticker = self._ticker(symbol)
        try:
            info = ticker.info or {}
        except Exception as exc:
            raise ProviderError(ProviderErrorCode.PROVIDER_ERROR, str(exc)) from exc
        snap = FundamentalsSnapshot(
            symbol=normalize_ticker_symbol(symbol),
            asset_type=asset_type,
            name=info.get("longName") or info.get("shortName"),
            exchange=info.get("exchange"),
            currency=info.get("currency"),
            market_cap=_to_float_or_none(info.get("marketCap")),
            circulating_cap=_to_float_or_none(info.get("sharesOutstanding") and
            float(info["sharesOutstanding"]) * _to_float_or_none(info.get("currentPrice")) or None),
            pe_ratio=_to_float_or_none(info.get("trailingPE")),
            pb_ratio=_to_float_or_none(info.get("priceToBook")),
            roe=_to_float_or_none(info.get("returnOnEquity")),
            revenue=_to_float_or_none(info.get("totalRevenue")),
            net_income=_to_float_or_none(info.get("netIncomeToCommon")),
            eps=_to_float_or_none(info.get("trailingEps")),
            dividend_yield=_to_float_or_none(info.get("dividendYield")),
            fifty_two_week_high=_to_float_or_none(info.get("fiftyTwoWeekHigh")),
            fifty_two_week_low=_to_float_or_none(info.get("fiftyTwoWeekLow")),
            as_of=datetime.now(timezone.utc),
            source=self.name,
            payload={
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "forward_pe": _to_float_or_none(info.get("forwardPE")),
                "peg_ratio": _to_float_or_none(info.get("pegRatio")),
                "beta": _to_float_or_none(info.get("beta")),
                "profit_margin": _to_float_or_none(info.get("profitMargins")),
                "operating_margin": _to_float_or_none(info.get("operatingMargins")),
                "debt_to_equity": _to_float_or_none(info.get("debtToEquity")),
                "free_cashflow": _to_float_or_none(info.get("freeCashflow")),
            },
        )
        return snap



def _to_float_or_none(value):
    """Coerce a yfinance .info field to float; return None on None / NaN / inf.

    yfinance sometimes returns ``math.nan`` or ``inf`` when the field
    is missing on the upstream; ``float()`` would happily serialise
    those as ``NaN`` JSON which is invalid in some clients. Treat them
    as missing here so the snapshot reports them as ``None``.
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    import math
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _info(ticker):
    try:
        return ticker.info or {}
    except Exception:
        return {}


def info_currency(ticker):
    return _info(ticker).get("currency")


def info_exchange(ticker):
    return _info(ticker).get("exchange")


def info_name(ticker):
    return _info(ticker).get("longName") or _info(ticker).get("shortName")
