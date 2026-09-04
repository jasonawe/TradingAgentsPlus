from __future__ import annotations

import contextlib
import io
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ..market_models import (
    AssetIdentity,
    Freshness,
    ProviderError,
    ProviderErrorCode,
    QuoteSnapshot,
)


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _split_a_share(symbol: str) -> tuple[str, str]:
    s = _normalize_symbol(symbol)
    if not (s.endswith(".SS") or s.endswith(".SZ")):
        raise ProviderError(
            ProviderErrorCode.INVALID_SYMBOL,
            f"akshare supports A-share symbols only: {s}",
        )
    code, suffix = s.split(".", 1)
    return code, suffix


def _exchange_for(symbol: str) -> str:
    _, suffix = _split_a_share(symbol)
    if suffix == "SS":
        return "SHH"
    if suffix == "SZ":
        return "SHZ"
    raise ProviderError(
        ProviderErrorCode.INVALID_SYMBOL,
        f"unsupported A-share suffix: {symbol}",
    )


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AKShareProvider:
    """Bulk A-share quote provider backed by AKShare (``stock_zh_a_spot_tx``).

    AKShare returns the full A-share market in a single call, so we cache the
    resulting dataframe for ``ttl_seconds`` and serve every ``get_quote`` from
    memory. The dataframe carries the full quantitative metric set:

      total market cap, turnover rate, volume ratio, P/E (TTM), amplitude,
      5/10/20/60-day change, year-to-date change, 52-week change, main-fund
      inflow, intraday price change speed.

    Volume is reported in 手 (lots); 1 手 = 100 shares for A-shares.
    Turnover is in 万元; market-cap fields are in 亿元. We convert to the
    raw units that QuoteSnapshot expects (shares / 元).
    """

    name = "akshare"

    def __init__(self, ttl_seconds: int = 60):
        self.ttl_seconds = max(15, int(ttl_seconds))
        self._cache: dict[str, Any] | None = None
        self._cache_time: float = 0.0
        self._lock = threading.Lock()

    def supports(self, symbol: str, asset_type: str, capability: str) -> bool:
        if asset_type != "stock":
            return False
        if capability not in {"quote", "identity"}:
            return False
        try:
            _split_a_share(symbol)
            return True
        except ProviderError:
            return False

    def _import_akshare(self):
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError(
                ProviderErrorCode.NOT_CONFIGURED,
                "akshare is not installed; pip install akshare",
            ) from exc
        return ak

    def _refresh(self) -> dict[str, Any]:
        ak = self._import_akshare()
        # AKShare prints progress bars via tqdm; redirect both stdout/stderr
        # so the web log stays quiet during cache fills.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            df = ak.stock_zh_a_spot_tx()
        if df is None or df.empty or "code" not in df.columns:
            raise ProviderError(
                ProviderErrorCode.PROVIDER_ERROR,
                "akshare returned no rows",
            )
        indexed: dict[str, Any] = {}
        for _, row in df.iterrows():
            raw_code = str(row.get("code") or "").strip()
            # AKShare returns codes like "sh600036" / "sz000001"; normalise
            # to the bare 6-digit form so callers can look up by ``.SS/.SZ``
            # ticker without juggling the prefix.
            bare = raw_code[2:] if raw_code[:2] in {"sh", "sz"} else raw_code
            if bare:
                indexed[bare] = row
        return indexed

    def _get_index(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache_time < self.ttl_seconds:
            return self._cache
        with self._lock:
            if self._cache is not None and now - self._cache_time < self.ttl_seconds:
                return self._cache
            indexed = self._refresh()
            self._cache = indexed
            self._cache_time = time.monotonic()
            return indexed

    def _row(self, symbol: str) -> tuple[str, Any]:
        code, _suffix = _split_a_share(symbol)
        index = self._get_index()
        row = index.get(code)
        if row is None:
            raise ProviderError(
                ProviderErrorCode.NO_DATA,
                f"akshare has no data for {code}",
            )
        return code, row

    def get_quote(self, symbol: str, asset_type: str) -> QuoteSnapshot:
        code, row = self._row(symbol)

        price = _to_float(row.get("zxj"))
        change = _to_float(row.get("zd"))
        change_percent = _to_float(row.get("zdf"))
        if price is not None and change is not None:
            previous_close = round(price - change, 4)
        else:
            previous_close = None

        # Volume is reported in 手 (lots). 1 手 = 100 shares for A-shares.
        volume_lots = _to_float(row.get("volume"))
        volume = volume_lots * 100 if volume_lots is not None else None

        # Turnover is reported in 万元 (ten-thousand yuan).
        turnover_wan = _to_float(row.get("turnover"))
        turnover = turnover_wan * 10_000 if turnover_wan is not None else None

        # Market cap fields are reported in 亿元 (hundred-million yuan).
        market_cap_yi = _to_float(row.get("zsz"))
        market_cap = market_cap_yi * 1e8 if market_cap_yi is not None else None

        circulating_cap_yi = _to_float(row.get("ltsz"))
        circulating_cap = (
            circulating_cap_yi * 1e8 if circulating_cap_yi is not None else None
        )

        as_of = datetime.now(timezone.utc)

        return QuoteSnapshot(
            symbol=_normalize_symbol(symbol),
            asset_type=asset_type,
            price=price,
            open=None,
            high=None,
            low=None,
            previous_close=previous_close,
            change=change,
            change_percent=change_percent,
            volume=volume,
            volume_ratio=_to_float(row.get("lb")),
            turnover=turnover,
            turnover_rate=_to_float(row.get("hsl")),
            market_cap=market_cap,
            circulating_cap=circulating_cap,
            pe_ratio=_to_float(row.get("pe_ttm")),
            amplitude=_to_float(row.get("zf")),
            currency="CNY",
            as_of=as_of,
            fetched_at=as_of,
            freshness=Freshness.FRESH,
            is_delayed=False,
            source=self.name,
            market_status="open",
            exchange=_exchange_for(symbol),
            raw_summary=str(row.get("name") or "") or None,
            payload={
                "zljlr": _to_float(row.get("zljlr")),
                "speed": _to_float(row.get("speed")),
                "zdf_d5": _to_float(row.get("zdf_d5")),
                "zdf_d10": _to_float(row.get("zdf_d10")),
                "zdf_d20": _to_float(row.get("zdf_d20")),
                "zdf_d60": _to_float(row.get("zdf_d60")),
                "zdf_w52": _to_float(row.get("zdf_w52")),
                "zdf_y": _to_float(row.get("zdf_y")),
            },
        )

    def get_identity(self, symbol: str, asset_type: str) -> AssetIdentity:
        code, row = self._row(symbol)
        return AssetIdentity(
            symbol=_normalize_symbol(symbol),
            asset_type=asset_type,
            name=str(row.get("name") or "") or None,
            exchange=_exchange_for(symbol),
            currency="CNY",
        )

    def get_candles(self, symbol: str, interval: str, start: Any, end: Any) -> list:
        raise ProviderError(
            ProviderErrorCode.PROVIDER_ERROR,
            "akshare candles not implemented; use yfinance",
        )
