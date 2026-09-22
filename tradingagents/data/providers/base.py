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
    FundamentalsSnapshot,
    ProviderError,
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

    # ------------------------------------------------------------------
    # §0.4.15 — fundamentals (PE / PB / market cap / ROE / ...)
    # ------------------------------------------------------------------
    # Default impl pulls from ``get_quote`` (which already carries
    # market_cap / circulating_cap / pe_ratio for akshare + eastmoney
    # A-share snapshots) and merges with ``get_identity`` for name /
    # exchange / currency. Providers that fetch fundamentals from a
    # different upstream (e.g. yfinance's ``ticker.info``) override
    # this to surface those richer fields (PB / ROE / EPS / etc.).
    def get_fundamentals(
        self, symbol: str, asset_type: str = "stock",
    ) -> FundamentalsSnapshot:
        """Return the fundamentals snapshot for ``symbol``.

        §0.4.15 — default impl merges :meth:`get_quote` (which carries
        market_cap / circulating_cap / pe_ratio on akshare + eastmoney
        A-share snapshots) with :meth:`get_identity` (name / exchange /
        currency). When NEITHER layer yields anything meaningful
        (every numeric field is ``None`` AND the provider doesn't even
        know the symbol's name) this raises :class:`ProviderError`
        with ``NO_DATA`` so :class:`ProviderFailover` walks to the
        next provider in the chain instead of returning an
        all-empty snapshot that would short-circuit failover.

        Providers that fetch fundamentals from a different upstream
        (e.g. yfinance's ``ticker.info``) override this to surface
        their richer fields (PB / ROE / EPS / 52-week range / etc.).
        """
        snap = FundamentalsSnapshot(
            symbol=str(symbol).strip().upper(), asset_type=asset_type,
        )
        identity_known = False
        # 1) Quote layer — fills market_cap / circulating_cap / pe_ratio
        #    on akshare + eastmoney; raises on US symbols those providers
        #    don't recognise.
        try:
            quote = self.get_quote(symbol, asset_type)
            snap.market_cap = quote.market_cap
            snap.circulating_cap = quote.circulating_cap
            snap.pe_ratio = quote.pe_ratio
            snap.amplitude = getattr(quote, "amplitude", None)
            snap.exchange = quote.exchange
            snap.currency = quote.currency
            snap.name = quote.raw_summary
            snap.as_of = quote.as_of
            snap.source = quote.source
        except ProviderError:
            pass
        except Exception:
            pass
        # 2) Identity layer — fills name / exchange / currency for
        #    providers that recognise the symbol even when quote fails.
        try:
            ident = self.get_identity(symbol, asset_type)
            snap.name = snap.name or ident.name
            snap.exchange = snap.exchange or ident.exchange
            snap.currency = snap.currency or ident.currency
            if ident.name or ident.exchange:
                identity_known = True
        except ProviderError:
            pass
        except Exception:
            pass
        # 3) Bail out if neither layer produced any fundamentals.
        #    §0.4.15 — even when the provider knows the symbol's
        #    name (e.g. eastmoney recognises 600036.SS as 招商银行),
        #    if it can't supply ANY fundamentals field the caller is
        #    still better served by walking to the next provider
        #    (yfinance reads ``ticker.info`` and has full PE/PB/
        #    market_cap). Without this, eastmoney would short-
        #    circuit the chain with a name-only snapshot.
        fundamentals_fields = (
            snap.market_cap, snap.circulating_cap, snap.pe_ratio,
            snap.pb_ratio, snap.roe, snap.revenue, snap.net_income,
            snap.eps, snap.dividend_yield,
            snap.fifty_two_week_high, snap.fifty_two_week_low,
        )
        if not any(v is not None for v in fundamentals_fields):
            from web.market_models import ProviderErrorCode
            raise ProviderError(
                ProviderErrorCode.NO_DATA,
                f"provider {self.name} has no fundamentals for {symbol}",
            )
        return snap

