from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Any, Callable

from .market_models import (
    AssetIdentity,
    BulkQuoteResponse,
    Candle,
    ProviderError,
    ProviderErrorCode,
    QuoteItem,
    QuoteItemError,
    QuoteSnapshot,
)


TRANSIENT = {
    ProviderErrorCode.NOT_CONFIGURED,
    ProviderErrorCode.RATE_LIMITED,
    ProviderErrorCode.TIMEOUT,
    ProviderErrorCode.PROVIDER_ERROR,
}


class ProviderRouter:
    STRATEGIES = {
        "default-yfinance": ("yfinance",),
        "fallback-yfinance-alpha-vantage": ("yfinance", "alpha_vantage"),
    }

    def __init__(self, providers: dict[str, Any], *, strategies: dict[str, tuple[str, ...]] | None = None):
        self.providers = providers
        self.strategies = strategies or self.STRATEGIES

    def chain(self, strategy: str) -> tuple[Any, ...]:
        names = self.strategies.get(strategy)
        if not names:
            raise ValueError(f"unknown quote strategy: {strategy}")
        return tuple(self.providers[name] for name in names if name in self.providers)

    def _call(self, method: str, symbol: str, asset_type: str, strategy: str, *args, timeout_seconds: float = 10, retries: int = 1):
        last: ProviderError | None = None
        names = self.strategies.get(strategy)
        if not names:
            raise ValueError(f"unknown quote strategy: {strategy}")
        for name in names:
            provider = self.providers.get(name)
            if provider is None:
                last = ProviderError(ProviderErrorCode.NOT_CONFIGURED, f"provider {name} unavailable")
                continue
            try:
                capability = {"get_quote": "quote", "get_candles": "candles", "get_identity": "identity"}[method]
                supports = getattr(provider, "supports", None)
                if not callable(supports) or not supports(symbol, asset_type, capability):
                    last = ProviderError(ProviderErrorCode.NOT_CONFIGURED, f"provider {name} does not support {capability}")
                    continue
                fn = getattr(provider, method, None)
                if not callable(fn):
                    last = ProviderError(ProviderErrorCode.NOT_CONFIGURED, f"provider {name} unavailable")
                    continue
                if method == "get_quote":
                    call = lambda: fn(symbol, asset_type)
                elif method == "get_identity":
                    call = lambda: fn(symbol, asset_type)
                else:
                    call = lambda: fn(symbol, *args)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(call)
                    try:
                        return future.result(timeout=timeout_seconds)
                    except FutureTimeout as exc:
                        future.cancel()
                        raise ProviderError(ProviderErrorCode.TIMEOUT, "provider request timed out") from exc
            except ProviderError as exc:
                last = exc
                if exc.code not in TRANSIENT:
                    raise
            except TimeoutError as exc:
                last = ProviderError(ProviderErrorCode.TIMEOUT, str(exc))
            except Exception as exc:
                last = ProviderError(ProviderErrorCode.PROVIDER_ERROR, str(exc))
        raise last or ProviderError(ProviderErrorCode.NO_DATA, "no provider available")

    def get_quote(self, symbol: str, asset_type: str, strategy: str = "default-yfinance", *, timeout_seconds: float = 10, retries: int = 1) -> QuoteSnapshot:
        return self._call("get_quote", symbol, asset_type, strategy, timeout_seconds=timeout_seconds, retries=retries)

    def get_candles(self, symbol: str, interval: str, start: Any, end: Any, strategy: str = "default-yfinance", *, timeout_seconds: float = 10, retries: int = 1) -> list[Candle]:
        return self._call("get_candles", symbol, "stock", strategy, interval, start, end, timeout_seconds=timeout_seconds, retries=retries)

    def get_identity(self, symbol: str, asset_type: str, strategy: str = "default-yfinance") -> AssetIdentity:
        return self._call("get_identity", symbol, asset_type, strategy)


class QuoteService:
    MAX_SYMBOLS = 50

    def __init__(self, router: ProviderRouter, repository: Any, *, clock: Callable[[], datetime] | None = None,
                 ttl_seconds: int | None = None, strategy: str | None = None, settings: Any | None = None,
                 config: dict[str, Any] | None = None, timeout_seconds: float = 10, retries: int = 1):
        self.router = router
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.settings = settings
        self.config = config or {}
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, int(retries))
        ttl_value = ttl_seconds if ttl_seconds is not None else self._resolved("quote_ttl_seconds", "TRADINGAGENTS_QUOTE_TTL_SECONDS", 60)
        self.ttl_seconds = max(15, min(60, int(ttl_value)))
        self.strategy = strategy or self._resolved("quote_strategy_id", "TRADINGAGENTS_QUOTE_STRATEGY", "default-yfinance")
        if self.strategy not in self.router.strategies:
            raise ValueError(f"unknown quote strategy: {self.strategy}")

    def _setting(self, key: str, fallback: Any) -> Any:
        if self.settings is not None:
            value = self.settings.get(key)
            if value is not None:
                return value.get("value") if isinstance(value, dict) else value
        return fallback

    def _resolved(self, key: str, env_key: str, default: Any) -> Any:
        value = self.config.get(key, default)
        value = self._setting(key, value)
        return os.getenv(env_key) or value

    def _cached(self, symbol: str, asset_type: str) -> QuoteSnapshot | None:
        row = self.repository.get_latest(symbol, asset_type)
        return QuoteSnapshot(**{**row, "payload": _payload(row)}) if row else None

    def get_quote(self, symbol: str, asset_type: str = "stock") -> QuoteSnapshot:
        cached = self._cached(symbol, asset_type)
        now = self.clock().astimezone(timezone.utc)
        if cached and cached.fetched_at and (now - cached.fetched_at).total_seconds() <= self.ttl_seconds:
            cached.cache_status = "hit"
            return cached
        try:
            fresh = self.router.get_quote(symbol, asset_type, self.strategy, timeout_seconds=self.timeout_seconds, retries=self.retries)
            fresh.cache_status = "live"
            self.repository.upsert_quote(fresh.model_dump(mode="json"))
            return fresh
        except ProviderError:
            if cached:
                age = (now - cached.fetched_at).total_seconds()
                cached.freshness = "stale" if age > self.ttl_seconds else cached.freshness
                cached.is_delayed = True
                cached.cache_status = "stale"
                return cached
            raise

    def get_quotes(self, symbols: list[str], asset_type: str = "stock") -> BulkQuoteResponse:
        if len(symbols) > self.MAX_SYMBOLS:
            raise ValueError("maximum 50 symbols")
        items: list[QuoteItem] = []
        for symbol in symbols:
            try:
                items.append(QuoteItem(symbol=symbol, quote=self.get_quote(symbol, asset_type)))
            except ProviderError as exc:
                items.append(QuoteItem(symbol=str(symbol).upper(), error=QuoteItemError(symbol=str(symbol).upper(), code=exc.code.value, message=exc.message)))
            except ValueError as exc:
                items.append(QuoteItem(symbol=str(symbol).upper(), error=QuoteItemError(symbol=str(symbol).upper(), code="invalid_symbol", message=str(exc))))
        return BulkQuoteResponse(items=items, partial=any(item.error is not None for item in items))


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    import json
    value = row.get("payload_json")
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}
