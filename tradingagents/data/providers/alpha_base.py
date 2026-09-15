"""AlphaProvider ABC (W3-D2 E3 — alpha_provider seam).

An ``AlphaProvider`` exposes historical OHLCV data that the alpha158
factor tools (``compute_alpha_factors``, ``evaluate_alpha``) feed into
the factor-computation layer in ``tradingagents.dataflows.alpha_factors``.
Factor formulas themselves stay local (numpy/pandas) — the seam is
about *where the OHLCV data comes from*, not the math.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional

import pandas as pd


class AlphaProvider(ABC):
    """Uniform historical-OHLCV provider interface."""

    #: Stable identifier used in registry keys, cache keys and UI.
    name: str = ""

    # ------------------------------------------------------------------
    # Required interface
    # ------------------------------------------------------------------
    @abstractmethod
    def supports(self, symbol: str, asset_type: str) -> bool:
        """True when this provider can serve OHLCV for ``symbol``."""

    @abstractmethod
    def load_ohlcv(
        self,
        symbol: str,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        asset_type: str = "stock",
    ) -> pd.DataFrame:
        """Return OHLCV DataFrame (Open/High/Low/Close/Volume) for ``symbol``.

        Implementations should return at least 5 years of daily bars so
        the factor library's rolling windows have enough samples. An
        empty DataFrame (no rows) is acceptable when upstream is
        genuinely empty.
        """

    # ------------------------------------------------------------------
    # Optional — overridable, sensible defaults
    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return {"name": self.name, "status": "configured"}

    def listed_symbols(self) -> Iterable[str]:
        return ()
