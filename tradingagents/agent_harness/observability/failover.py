"""Provider failover (v3 spec §7.2 #2).

Try ``primary`` provider first; on transient ``ProviderError``,
fall back to the next registered provider. Terminal errors
(``INVALID_SYMBOL`` / ``NO_DATA``) propagate immediately.
"""
from __future__ import annotations

import logging
from typing import Any

from tradingagents.data.providers.registry import PROVIDERS
from web.market_models import ProviderError, ProviderErrorCode

LOGGER = logging.getLogger(__name__)

_TRANSIENT_CODES = {
    ProviderErrorCode.TIMEOUT,
    ProviderErrorCode.RATE_LIMITED,
    ProviderErrorCode.PROVIDER_ERROR,
}


def _auto_fallback_chain(primary: str) -> list[str]:
    """Return fallback names in priority order (excluding ``primary``).

    Order rationale: the four registered providers cover different upstream
    ecosystems — putting ``yfinance`` first for non-A-share symbols and
    ``eastmoney`` second is fine because they're geographically separated,
    but for A-share defaults we want EastMoney → AKShare (both Chinese
    sources) → yfinance (delayed) → alpha_vantage (foreign key). This
    heuristic keeps the typical happy-path short while still giving
    every provider a chance when the upstream is genuinely down.
    """
    base = ("yfinance", "akshare", "eastmoney", "alpha_vantage")
    return [n for n in base if n != primary and n in PROVIDERS]


class ProviderFailover:
    """Wrap a primary provider with automatic fallback.

    When ``primary`` is ``None`` we read the process-active provider from
    :func:`registry.get_active_provider_name`, so the failover naturally
    tracks whatever the settings UI has the user on.
    """

    def __init__(
        self,
        primary: str | None = None,
        fallbacks: list[str] | None = None,
    ) -> None:
        if primary is None:
            from tradingagents.data.providers.registry import get_active_provider_name
            primary = get_active_provider_name()
        self.primary_name = primary
        if fallbacks is None:
            fallbacks = _auto_fallback_chain(primary)
        self.fallback_names = fallbacks
        # Name of the provider that actually answered the most recent
        # successful ``call()``. ``None`` until the first success.
        self.last_used_name: str | None = None

    def _chain(self) -> list[str]:
        return [self.primary_name, *self.fallback_names]

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        last_err: BaseException | None = None
        for name in self._chain():
            provider = PROVIDERS.get(name)
            if provider is None:
                continue
            fn = getattr(provider, method, None)
            if not callable(fn):
                continue
            try:
                result = fn(*args, **kwargs)
            except ProviderError as e:
                if e.code not in _TRANSIENT_CODES:
                    raise
                last_err = e
                LOGGER.warning("provider %s.%s transient error: %s; trying fallback", name, method, e)
                continue
            except Exception as e:
                last_err = e
                LOGGER.warning("provider %s.%s unknown error: %s; trying fallback", name, method, e)
                continue
            # Success — record which provider actually served the request
            # so callers can report accurate provenance (e.g. when the
            # primary was unhealthy and a fallback answered).
            self.last_used_name = name
            return result
        if last_err:
            raise last_err
        raise RuntimeError("no providers available")
