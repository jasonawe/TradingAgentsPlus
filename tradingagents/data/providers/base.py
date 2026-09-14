"""Provider ABC (N59 fix).

All market-data providers MUST subclass :class:`Provider` so the registry,
the cache layer, and the route layer can treat them uniformly. The shape
mirrors the existing ``QuoteProvider`` Protocol in ``web/market_models``
plus three extra axes (``name``, ``capabilities``, ``health``) borrowed
from OpenBB's ``OBBject``/provider pattern.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional

from web.market_models import (
    AssetIdentity,
    Candle,
    QuoteSnapshot,
)


class Provider(ABC):
    """Uniform market-data provider interface."""

    #: Stable identifier used in cache keys, registry and telemetry.
    name: str = ""

    #: Optional capability map (``asset_type -> set[str]``) used by the
    #: registry to pick the best provider when ``active_provider`` is
    #: set to ``"auto"``. Defaults to "supports everything".
    capabilities: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Required interface
    # ------------------------------------------------------------------
    @abstractmethod
    def supports(self, symbol: str, asset_type: str, capability: str) -> bool:
        """Return True when this provider can serve ``capability`` for the asset."""

    @abstractmethod
    def get_quote(self, symbol: str, asset_type: str) -> QuoteSnapshot:
        """Return the latest quote snapshot for ``symbol``."""

    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        interval: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        asset_type: str = "stock",
    ) -> list[Candle]:
        """Return OHLCV candles for ``symbol`` in the given window."""

    @abstractmethod
    def get_identity(self, symbol: str, asset_type: str) -> AssetIdentity:
        """Return static identity fields (name, exchange, currency, ...)."""

    # ------------------------------------------------------------------
    # Optional interface — overridable but with sensible defaults
    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """Lightweight self-check used by ``/api/providers/health``.

        Subclasses can override to ping upstream endpoints; the default
        is "configured" so a freshly imported provider reports OK.
        """
        return {"name": self.name, "status": "configured"}

    def listed_symbols(self) -> Iterable[str]:
        """Optional: return a list of symbols the provider can quote."""
        return ()
