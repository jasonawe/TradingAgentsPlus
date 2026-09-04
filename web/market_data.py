from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FutureTimeout
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from typing import cast

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


class _DaemonExecutor:
    """Fixed-size daemon workers; hung provider calls cannot block process exit."""

    def __init__(self, max_workers: int = 8):
        self._queue = queue.Queue()
        self._workers = []
        for index in range(max_workers):
            worker = threading.Thread(
                target=self._run, name=f"market-provider-{index}", daemon=True
            )
            worker.start()
            self._workers.append(worker)

    def submit(self, fn):
        future = Future()
        self._queue.put((future, fn))
        return future

    def _run(self):
        while True:
            future, fn = self._queue.get()
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn())
                except BaseException as exc:
                    future.set_exception(exc)
            self._queue.task_done()


_BOUNDED_EXECUTOR = _DaemonExecutor()


class ProviderRouter:
    def __init__(
        self,
        providers: dict[str, Any],
        *,
        strategies: dict[str, tuple[str, ...]] | None = None,
        health: Any | None = None,
    ):
        self.providers = providers
        # Lazy import keeps the router usable in contexts where config.py is not
        # importable (e.g. unit tests) and lets the chain follow the catalog.
        if strategies is None:
            from .config import QUOTE_STRATEGIES
            strategies = {k: tuple(v["providers"]) for k, v in QUOTE_STRATEGIES.items()}
        self.strategies = strategies
        self.health = health

    def chain(self, strategy: str) -> tuple[Any, ...]:
        names = self.strategies.get(strategy)
        if not names:
            raise ValueError(f"unknown quote strategy: {strategy}")
        return tuple(self.providers[name] for name in names if name in self.providers)

    def _call(
        self,
        method: str,
        symbol: str,
        asset_type: str,
        strategy: str,
        *args,
        timeout_seconds: float = 10,
        retries: int = 1,
    ):
        last: ProviderError | None = None
        names = self.strategies.get(strategy)
        if not names:
            raise ValueError(f"unknown quote strategy: {strategy}")
        for name in names:
            provider = self.providers.get(name)
            if provider is None:
                last = ProviderError(
                    ProviderErrorCode.NOT_CONFIGURED, f"provider {name} unavailable"
                )
                self._record_failure(name, last, 0.0)
                continue
            try:
                capability = {
                    "get_quote": "quote",
                    "get_candles": "candles",
                    "get_identity": "identity",
                }[method]
                supports = getattr(provider, "supports", None)
                if not callable(supports) or not supports(symbol, asset_type, capability):
                    last = ProviderError(
                        ProviderErrorCode.NOT_CONFIGURED,
                        f"provider {name} does not support {capability}",
                    )
                    self._record_failure(name, last, 0.0)
                    continue
                fn = getattr(provider, method, None)
                if not callable(fn):
                    last = ProviderError(
                        ProviderErrorCode.NOT_CONFIGURED, f"provider {name} unavailable"
                    )
                    self._record_failure(name, last, 0.0)
                    continue
                if method == "get_quote" or method == "get_identity":

                    def call(fn=fn, symbol=symbol, asset_type=asset_type):
                        return fn(symbol, asset_type)
                else:

                    def call(fn=fn, symbol=symbol, args=args):
                        return fn(symbol, *args)

                for attempt in range(max(0, retries) + 1):
                    started = time.monotonic()
                    try:
                        future = _BOUNDED_EXECUTOR.submit(call)
                        result = future.result(timeout=timeout_seconds)
                        self._record_success(
                            name, (time.monotonic() - started) * 1000
                        )
                        return result
                    except FutureTimeout as exc:
                        future.cancel()
                        error = ProviderError(
                            ProviderErrorCode.TIMEOUT,
                            "provider request timed out",
                        )
                        self._record_failure(
                            name, error, (time.monotonic() - started) * 1000
                        )
                        if attempt < retries:
                            continue
                        raise error from exc
                    except ProviderError as exc:
                        self._record_failure(
                            name, exc, (time.monotonic() - started) * 1000
                        )
                        if attempt < retries and exc.code in TRANSIENT:
                            continue
                        raise
                    except Exception as exc:
                        error = ProviderError(ProviderErrorCode.PROVIDER_ERROR, str(exc))
                        self._record_failure(
                            name, error, (time.monotonic() - started) * 1000
                        )
                        raise error from exc
            except ProviderError as exc:
                last = exc
                if exc.code not in TRANSIENT:
                    raise
            except TimeoutError as exc:
                last = ProviderError(ProviderErrorCode.TIMEOUT, str(exc))
            except Exception as exc:
                last = ProviderError(ProviderErrorCode.PROVIDER_ERROR, str(exc))
        raise last or ProviderError(ProviderErrorCode.NO_DATA, "no provider available")

    def _record_success(self, provider: str, latency_ms: float) -> None:
        if self.health is None:
            return
        try:
            self.health.record_success(provider, latency_ms)
        except Exception:
            return

    def _record_failure(
        self, provider: str, error: ProviderError, latency_ms: float
    ) -> None:
        if self.health is None:
            return
        try:
            if error.code is ProviderErrorCode.NOT_CONFIGURED:
                self.health.mark_not_configured(
                    provider,
                    error.message,
                    latency_ms=latency_ms,
                    count_request=True,
                )
            else:
                self.health.record_failure(
                    provider,
                    error.code.value,
                    error.message,
                    latency_ms,
                )
        except Exception:
            return

    def get_quote(
        self,
        symbol: str,
        asset_type: str,
        strategy: str = "default-eastmoney",
        *,
        timeout_seconds: float = 10,
        retries: int = 1,
    ) -> QuoteSnapshot:
        return self._call(
            "get_quote",
            symbol,
            asset_type,
            strategy,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )

    def get_candles(
        self,
        symbol: str,
        interval: str,
        start: Any,
        end: Any,
        strategy: str = "default-eastmoney",
        *,
        timeout_seconds: float = 10,
        retries: int = 1,
    ) -> list[Candle]:
        return self._call(
            "get_candles",
            symbol,
            "stock",
            strategy,
            interval,
            start,
            end,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )

    def get_identity(
        self,
        symbol: str,
        asset_type: str,
        strategy: str = "default-eastmoney",
        *,
        timeout_seconds: float = 10,
        retries: int = 1,
    ) -> AssetIdentity:
        return self._call(
            "get_identity",
            symbol,
            asset_type,
            strategy,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )


class QuoteService:
    MAX_SYMBOLS = 50

    def __init__(
        self,
        router: ProviderRouter,
        repository: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl_seconds: int | None = None,
        strategy: str | None = None,
        settings: Any | None = None,
        config: dict[str, Any] | None = None,
        timeout_seconds: float = 10,
        retries: int = 1,
    ):
        self.router = router
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.settings = settings
        self.config = config or {}
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, int(retries))
        ttl_value = (
            ttl_seconds
            if ttl_seconds is not None
            else self._resolved("quote_ttl_seconds", "TRADINGAGENTS_QUOTE_TTL_SECONDS", 60)
        )
        self.ttl_seconds = max(15, min(60, int(ttl_value)))
        self.strategy = strategy or self._resolved(
            "quote_strategy_id", "TRADINGAGENTS_QUOTE_STRATEGY", "default-eastmoney"
        )
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
        if not row:
            return None
        data = {**row, "payload": _payload(row)}
        data.pop("payload_json", None)
        return QuoteSnapshot(**data)

    def get_quote(self, symbol: str, asset_type: str = "stock") -> QuoteSnapshot:
        cached = self._cached(symbol, asset_type)
        now = self.clock().astimezone(timezone.utc)
        if cached:
            cached.stale_seconds = self._quote_age(cached, now)
        if (
            cached
            and cached.stale_seconds is not None
            and cached.stale_seconds <= self.ttl_seconds
        ):
            cached.cache_status = "hit"
            cached.provider_status = "ready"
            return cached
        try:
            fresh = self.router.get_quote(
                symbol,
                asset_type,
                self.strategy,
                timeout_seconds=self.timeout_seconds,
                retries=self.retries,
            )
            fresh.cache_status = "live"
            fresh.provider_status = "ready"
            fresh.stale_seconds = self._quote_age(fresh, now)
            self.repository.upsert_quote(fresh.model_dump(mode="json"))
            return fresh
        except ProviderError:
            if cached:
                age = cached.stale_seconds or 0
                cached.freshness = "stale" if age > self.ttl_seconds else cached.freshness
                cached.is_delayed = True
                cached.cache_status = "hit"
                cached.provider_status = "degraded"
                cached.stale_seconds = self._quote_age(cached, now)
                return cached
            raise

    @staticmethod
    def _quote_age(quote: QuoteSnapshot, now: datetime) -> int | None:
        observed = quote.as_of or quote.fetched_at
        if observed is None:
            return None
        return max(0, int((now - observed).total_seconds()))

    def get_quotes(self, symbols: list[str], asset_type: str = "stock") -> BulkQuoteResponse:
        if len(symbols) > self.MAX_SYMBOLS:
            raise ValueError("maximum 50 symbols")
        # Pre-allocate so order matches the input ``symbols`` regardless of which upstream
        # call returns last. Sequential lookups (single symbol) skip the executor entirely
        # to keep the existing tight path unchanged.
        items: list[QuoteItem | None] = [None] * len(symbols)

        def fetch_one(idx: int, symbol: str) -> None:
            normalized = str(symbol).upper()
            try:
                snapshot = self.get_quote(symbol, asset_type)
                items[idx] = QuoteItem(symbol=normalized, quote=snapshot)
                return
            except ProviderError as exc:
                provider_status = (
                    "not_configured"
                    if exc.code is ProviderErrorCode.NOT_CONFIGURED
                    else "degraded"
                )
                unavailable = QuoteSnapshot(
                    symbol=normalized,
                    asset_type=asset_type,
                    fetched_at=self.clock(),
                    freshness="unavailable",
                    cache_status="miss",
                    provider_status=provider_status,
                )
                items[idx] = QuoteItem(
                    symbol=normalized,
                    quote=unavailable,
                    error=QuoteItemError(
                        symbol=normalized,
                        code=exc.code.value,
                        message=_diagnostic(exc.code),
                    ),
                )
                return
            except ValueError:
                unavailable = QuoteSnapshot(
                    symbol=normalized,
                    asset_type=asset_type,
                    fetched_at=self.clock(),
                    freshness="unavailable",
                    cache_status="miss",
                    provider_status="ready",
                )
                items[idx] = QuoteItem(
                    symbol=normalized,
                    quote=unavailable,
                    error=QuoteItemError(
                        symbol=normalized,
                        code="invalid_symbol",
                        message=_diagnostic(ProviderErrorCode.INVALID_SYMBOL),
                    ),
                )

        if len(symbols) <= 1:
            for idx, symbol in enumerate(symbols):
                fetch_one(idx, symbol)
        else:
            # yfinance HTTP I/O releases the GIL, so a bounded thread pool parallelises
            # upstream fetches without ordering surprises. ``max_workers`` is capped at 8
            # to keep upstream rate-limits predictable for free-tier providers.
            max_workers = min(len(symbols), 8)
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="quote-bulk") as executor:
                futures = [
                    executor.submit(fetch_one, idx, symbol)
                    for idx, symbol in enumerate(symbols)
                ]
                for fut in futures:
                    # ``fetch_one`` swallows the expected error classes; this ``result()``
                    # surfaces anything unexpected (e.g. a DB write failure inside get_quote).
                    fut.result()
        return BulkQuoteResponse(
            items=cast(list[QuoteItem], items),
            partial=any(item.error is not None for item in items if item is not None),
        )


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    import json

    value = row.get("payload_json")
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _diagnostic(code: ProviderErrorCode) -> str:
    return {
        ProviderErrorCode.NOT_CONFIGURED: "行情数据源未配置",
        ProviderErrorCode.RATE_LIMITED: "行情数据源请求频繁，请稍后重试",
        ProviderErrorCode.TIMEOUT: "行情数据源请求超时",
        ProviderErrorCode.NO_DATA: "暂未找到行情数据",
        ProviderErrorCode.INVALID_SYMBOL: "资产代码无效",
        ProviderErrorCode.PROVIDER_ERROR: "行情数据源暂时不可用",
    }.get(code, "行情数据暂时不可用")
