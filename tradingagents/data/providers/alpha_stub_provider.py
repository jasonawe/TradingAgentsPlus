"""Stub alpha provider (W3-D2 E3).

Returns an empty OHLCV DataFrame so the alpha tools can be exercised
end-to-end without any upstream data. Mirrors the pre-seam behaviour
where ``list_alpha_factors`` returned a fixed list of placeholder
factor names and ``compute_alpha_factors`` returned zeros.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

import pandas as pd

from .alpha_base import AlphaProvider


class StubAlphaProvider(AlphaProvider):
    name = "stub"

    def supports(self, symbol: str, asset_type: str) -> bool:
        # Stub supports everything — it's the universal fallback.
        return bool(symbol)

    def load_ohlcv(
        self,
        symbol: str,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        asset_type: str = "stock",
    ) -> pd.DataFrame:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
