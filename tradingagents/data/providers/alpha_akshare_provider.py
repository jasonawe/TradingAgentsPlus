"""AKShare-backed alpha provider (W3-D2 E3).

AKShare provides A-share historical bars via ``stock_zh_a_hist``. The
provider tries to import akshare lazily and returns an empty DataFrame
with a warning if akshare is not installed — keeps the seam alive in
dev / CI environments where the dependency is optional.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import pandas as pd

from .alpha_base import AlphaProvider

LOGGER = logging.getLogger(__name__)


class AKShareAlphaProvider(AlphaProvider):
    name = "akshare"

    def supports(self, symbol: str, asset_type: str) -> bool:
        if not symbol:
            return False
        if asset_type != "stock":
            return False
        # A-share only (suffix-aware); non-A-share tickers should be
        # served by yfinance / alpha_vantage.
        s = symbol.upper()
        return s.endswith(".SS") or s.endswith(".SZ")

    def load_ohlcv(
        self,
        symbol: str,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        asset_type: str = "stock",
    ) -> pd.DataFrame:
        try:
            import akshare as ak  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dep
            LOGGER.info("akshare not installed: %s", exc)
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        # A-share: strip .SS / .SZ for akshare
        raw_symbol = symbol.upper()
        if raw_symbol.endswith(".SS"):
            ak_symbol = "sh" + raw_symbol[:-3]
        elif raw_symbol.endswith(".SZ"):
            ak_symbol = "sz" + raw_symbol[:-3]
        else:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        end_str = end or datetime.now(timezone.utc).date().isoformat()
        if start is None:
            start_dt = datetime.now(timezone.utc).date() - timedelta(days=365 * 5)
            start_str = start_dt.isoformat()
        else:
            start_str = start

        try:
            df = ak.stock_zh_a_hist(
                symbol=ak_symbol, period="daily",
                start_date=start_str.replace("-", ""),
                end_date=end_str.replace("-", ""),
                adjust="qfq",
            )
        except Exception as exc:
            LOGGER.warning("akshare stock_zh_a_hist failed for %s: %s", symbol, exc)
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        if df is None or df.empty:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        # akshare columns: 日期,开盘,收盘,最高,最低,成交量,...
        rename = {
            "日期": "Date", "开盘": "Open", "收盘": "Close",
            "最高": "High", "最低": "Low", "成交量": "Volume",
        }
        df = df.rename(columns=rename)
        keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
        return df[keep].reset_index(drop=True)
