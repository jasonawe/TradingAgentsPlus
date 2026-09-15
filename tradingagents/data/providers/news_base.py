"""NewsProvider ABC (W3-D2 E2 — news_provider seam).

Mirrors the ``Provider`` ABC shape used for quote data (E1) so the
registry, settings UI, and route layer can treat news backends
uniformly. Concrete providers wrap the existing dataflow fetchers
(``get_news_yfinance``, ``get_news`` alpha_vantage, stub) without
forcing the harness tool to know which one is in use.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable

from .news_schema import NewsArticle, NewsWindow


class NewsProvider(ABC):
    """Uniform news provider interface.

    Returns :class:`NewsArticle` records in a :class:`NewsWindow` for a
    given ``(symbol, lookback_days)`` query. Implementations are free
    to filter by symbol/date/limit however they like, but MUST return
    a ``NewsWindow`` even when no articles are found.
    """

    #: Stable identifier used in registry keys, cache keys and UI.
    name: str = ""

    # ------------------------------------------------------------------
    # Required interface
    # ------------------------------------------------------------------
    @abstractmethod
    def supports(self, symbol: str, asset_type: str) -> bool:
        """True when this provider can serve news for ``symbol``."""

    @abstractmethod
    def get_news(
        self, symbol: str, *, days: int = 7, asset_type: str = "stock",
    ) -> NewsWindow:
        """Return a news window for ``symbol`` covering the last ``days`` days."""

    # ------------------------------------------------------------------
    # Optional — overridable, sensible defaults
    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return {"name": self.name, "status": "configured"}

    def listed_symbols(self) -> Iterable[str]:
        return ()
