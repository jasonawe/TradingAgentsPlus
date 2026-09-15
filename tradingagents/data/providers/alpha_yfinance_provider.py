"""YFinance-backed alpha provider (W3-D2 E3).

Wraps the existing ``tradingagents.dataflows.stockstats_utils.load_ohlcv``
fetcher (which already handles 5-year cache, curr_date filtering, and
broker/forex symbol resolution). The seam simply gives alpha tools a
name-based handle so future providers (akshare, qlib) can be swapped
in via the registry.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import pandas as pd

from .alpha_base import AlphaProvider

LOGGER = logging.getLogger(__name__)


class YFinanceAlphaProvider(AlphaProvider):
    name = "yfinance"

    def supports(self, symbol: str, asset_type: str) -> bool:
        if not symbol:
            return False
        return asset_type in ("stock", "crypto", "fund")

    def load_ohlcv(
        self,
        symbol: str,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        asset_type: str = "stock",
    ) -> pd.DataFrame:
        try:
            from tradingagents.dataflows.stockstats_utils import load_ohlcv
        except Exception as exc:  # pragma: no cover - import error path
            LOGGER.warning("yfinance alpha import failed: %s", exc)
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        # ``load_ohlcv`` uses an internal 5y cache; ``curr_date`` is the
        # look-ahead bound. We pass today's UTC date so no rows are
        # filtered out for callers that want the freshest data.
        curr_date = (end or datetime.now(timezone.utc).date().isoformat())
        try:
            df = load_ohlcv(symbol=symbol, curr_date=curr_date)
        except Exception as exc:
            LOGGER.warning("yfinance alpha load_ohlcv failed for %s: %s", symbol, exc)
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        if df is None or df.empty:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        return df
