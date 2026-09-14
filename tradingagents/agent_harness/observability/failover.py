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


class ProviderFailover:
    """Wrap a primary provider with automatic fallback."""

    def __init__(self, primary: str = "eastmoney", fallbacks: list[str] | None = None) -> None:
        self.primary_name = primary
        if fallbacks is None:
            fallbacks = [n for n in ("yfinance", "akshare", "alpha_vantage") if n != primary]
        self.fallback_names = fallbacks

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
                return fn(*args, **kwargs)
            except ProviderError as e:
                if e.code not in _TRANSIENT_CODES:
                    raise
                last_err = e
                LOGGER.warning("provider %s.%s transient error: %s; trying fallback", name, method, e)
            except Exception as e:
                last_err = e
                LOGGER.warning("provider %s.%s unknown error: %s; trying fallback", name, method, e)
        if last_err:
            raise last_err
        raise RuntimeError("no providers available")
