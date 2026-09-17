"""QuoteService singleflight: collapse concurrent requests for the same
(symbol, asset_type, force_refresh) into a single upstream call.

Prevents the background prewarmer and an in-flight user request from both
hitting the upstream provider at the same moment.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

import pytest

from web.market_data import ProviderRouter, QuoteService
from web.market_models import (
    AssetIdentity,
    ProviderError,
    ProviderErrorCode,
    QuoteSnapshot,
)


def _snapshot(symbol: str, ts: datetime) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol,
        asset_type="stock",
        name=symbol,
        exchange="X",
        currency="USD",
        price=100.0,
        previous_close=99.0,
        change=1.0,
        change_pct=1.01,
        open=99.5,
        high=100.5,
        low=98.5,
        volume=1000,
        turnover=None,
        market_cap=None,
        pe_ttm=None,
        pb_mrq=None,
        fetched_at=ts,
        as_of=ts,
        freshness="fresh",
        is_delayed=False,
        cache_status="live",
        provider_status="ready",
        source="yfinance",
        warnings=[],
    )


class _CountingProvider:
    """Provider that counts upstream calls and can be artificially slowed."""

    def __init__(self, sleep_seconds: float = 0.0, raise_with: ProviderError | None = None):
        self.calls = 0
        self.sleep_seconds = sleep_seconds
        self.raise_with = raise_with
        self.lock = threading.Lock()
        self.call_started = threading.Event()
        self.can_proceed = threading.Event()

    def supports(self, *args, **kwargs) -> bool:
        return True

    def get_quote(self, symbol, asset_type):
        with self.lock:
            self.calls += 1
        # Mimic real HTTP I/O: yfinance/efinance take 50-300ms; a 100ms sleep
        # is enough to release the GIL so concurrent callers can register as
        # waiters inside the singleflight map.
        time.sleep(0.1)
        if self.sleep_seconds > 0:
            # Block until the test releases us so we can observe concurrent callers.
            self.call_started.set()
            if not self.can_proceed.wait(timeout=self.sleep_seconds + 5):
                raise ProviderError(ProviderErrorCode.TIMEOUT, "test gate timeout")
            time.sleep(self.sleep_seconds)
        if self.raise_with is not None:
            # Mimic the real upstream timeout window: providers usually take
            # 1-5s before failing, so the waiter has plenty of time to register.
            time.sleep(0.1)
            raise self.raise_with
        return _snapshot(symbol, datetime(2026, 9, 17, tzinfo=timezone.utc))

    def get_candles(self, *args, **kwargs):
        return []

    def get_identity(self, symbol, asset_type):
        return AssetIdentity(symbol=symbol, asset_type=asset_type, name=symbol)


class _MemRepo:
    """In-memory quote repository — no SQLite, no daemon threads."""

    def __init__(self):
        self._rows: dict[tuple[str, str], dict] = {}

    def get_latest(self, symbol: str, asset_type: str):
        return self._rows.get((symbol, asset_type))

    def upsert_quote(self, payload: dict):
        key = (payload["symbol"], payload["asset_type"])
        self._rows[key] = {**payload}
        return payload


@pytest.fixture
def service():
    def _build(provider, *, ttl_seconds: int = 0):
        repo = _MemRepo()
        # ``ttl_seconds=0`` forces a refetch on every call so singleflight
        # is exercised even when the underlying router is fast.
        # ``retries=0`` keeps the assertion ``provider.calls == N`` readable
        # — with the default ``retries=1`` the router retries TRANSIENT errors
        # so a single fetcher call becomes 2 provider calls.
        return QuoteService(
            ProviderRouter({"yfinance": provider}),
            repo,
            clock=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc),
            ttl_seconds=ttl_seconds,
            timeout_seconds=10,
            retries=0,
        )

    return _build


def test_two_concurrent_same_key_one_upstream_call(service):
    provider = _CountingProvider()
    svc = service(provider)
    with ThreadPoolExecutor(max_workers=4) as pool:
        f1 = pool.submit(svc.get_quote, "AAPL", "stock")
        f2 = pool.submit(svc.get_quote, "AAPL", "stock")
        s1, s2 = f1.result(timeout=10), f2.result(timeout=10)
    assert s1.symbol == "AAPL" and s2.symbol == "AAPL"
    assert s1 is s2 or s1.price == s2.price  # both see the same fetch
    assert provider.calls == 1, "second caller should have joined the in-flight fetch"


def test_three_different_keys_three_upstream_calls(service):
    provider = _CountingProvider()
    svc = service(provider)
    with ThreadPoolExecutor(max_workers=4) as pool:
        wait([
            pool.submit(svc.get_quote, "AAPL", "stock"),
            pool.submit(svc.get_quote, "MSFT", "stock"),
            pool.submit(svc.get_quote, "NVDA", "stock"),
        ])
    assert provider.calls == 3


def test_force_refresh_creates_separate_key(service):
    provider = _CountingProvider()
    svc = service(provider)
    with ThreadPoolExecutor(max_workers=4) as pool:
        wait([
            pool.submit(svc.get_quote, "AAPL", "stock", force_refresh=False),
            pool.submit(svc.get_quote, "AAPL", "stock", force_refresh=True),
        ])
    # prewarmer (force=False) and explicit refresh (force=True) must not coalesce.
    assert provider.calls == 2


def test_provider_error_with_cache_propagates_degraded_to_all_waiters(service):
    provider = _CountingProvider(raise_with=ProviderError(ProviderErrorCode.TIMEOUT, "offline"))
    svc = service(provider, ttl_seconds=60)
    # Pre-populate cache so the fallback path is exercised.
    fresh_ts = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)
    cached = _snapshot("AAPL", fresh_ts)
    svc.repository.upsert_quote(cached.model_dump(mode="json"))
    # Clock is fixed at 2026-09-17 00:00 so the cache is fresh for TTL purposes.
    with ThreadPoolExecutor(max_workers=4) as pool:
        f1 = pool.submit(svc.get_quote, "AAPL", "stock", force_refresh=True)
        f2 = pool.submit(svc.get_quote, "AAPL", "stock", force_refresh=True)
        s1, s2 = f1.result(timeout=10), f2.result(timeout=10)
    assert provider.calls == 1
    assert s1.provider_status == "degraded"
    assert s2.provider_status == "degraded"
    assert s1.is_delayed is True and s2.is_delayed is True


def test_provider_error_without_cache_propagates_exception(service):
    provider = _CountingProvider(raise_with=ProviderError(ProviderErrorCode.TIMEOUT, "offline"))
    svc = service(provider, ttl_seconds=60)
    with ThreadPoolExecutor(max_workers=4) as pool:
        f1 = pool.submit(svc.get_quote, "AAPL", "stock", force_refresh=True)
        f2 = pool.submit(svc.get_quote, "AAPL", "stock", force_refresh=True)
        with pytest.raises(ProviderError):
            f1.result(timeout=10)
        with pytest.raises(ProviderError):
            f2.result(timeout=10)
    assert provider.calls == 1


def test_after_completion_new_caller_does_not_register_inflight(service):
    """Singleflight only collapses in-flight requests; a fresh call after
    completion must not be blocked by a stale entry."""
    provider = _CountingProvider()
    svc = service(provider)
    # First call completes, inflight entry is unregistered.
    svc.get_quote("AAPL", "stock", force_refresh=True)
    assert provider.calls == 1
    # Second call uses force_refresh=True so cache TTL doesn't short-circuit.
    svc.get_quote("AAPL", "stock", force_refresh=True)
    assert provider.calls == 2
    assert svc._inflight == {}, "completed entry must be cleaned up"


def test_waiter_unblocks_after_fetcher_publishes(service):
    """The waiter must not block forever — once the fetcher publishes, the
    Event fires and the waiter returns the shared result."""
    provider = _CountingProvider(sleep_seconds=0.1)
    svc = service(provider)
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        f1 = pool.submit(svc.get_quote, "AAPL", "stock", force_refresh=True)
        # Yield so the fetcher registers first.
        time.sleep(0.1)
        f2 = pool.submit(svc.get_quote, "AAPL", "stock", force_refresh=True)
        # Let the provider's gate release so the fetcher returns.
        if provider.call_started.wait(timeout=2):
            provider.can_proceed.set()
        s1, s2 = f1.result(timeout=10), f2.result(timeout=10)
    elapsed = time.monotonic() - start
    assert provider.calls == 1
    # Both calls returned within the fetcher's runtime plus a small slack,
    # not 2x — proving the waiter piggy-backed on the fetcher.
    assert elapsed < 0.6, f"waiter appears to have refetched (elapsed={elapsed:.2f}s)"
    assert s1.price == s2.price == 100.0


def test_bulk_path_collapses_repeated_symbols(service):
    """``get_quotes`` fans out per-symbol — duplicates inside one bulk request
    must collapse at the singleflight layer."""
    provider = _CountingProvider()
    svc = service(provider)
    resp = svc.get_quotes(["AAPL", "MSFT", "AAPL", "AAPL"])
    assert provider.calls == 2, "duplicate AAPL in bulk should collapse to 1 call"
    assert [item.symbol for item in resp.items] == ["AAPL", "MSFT", "AAPL", "AAPL"]


def test_concurrent_bulk_requests_collapse(service):
    provider = _CountingProvider()
    svc = service(provider)
    with ThreadPoolExecutor(max_workers=4) as pool:
        wait([
            pool.submit(svc.get_quotes, ["AAPL", "MSFT"]),
            pool.submit(svc.get_quotes, ["AAPL", "MSFT"]),
        ])
    assert provider.calls == 2, "concurrent bulks for the same symbols collapse to 1 fetch each"


def test_no_inflight_leak_on_provider_success(service):
    provider = _CountingProvider()
    svc = service(provider)
    with ThreadPoolExecutor(max_workers=4) as pool:
        wait([pool.submit(svc.get_quote, s, "stock") for s in ("A", "B", "C", "D")])
    assert svc._inflight == {}


def test_no_inflight_leak_on_provider_failure(service):
    provider = _CountingProvider(raise_with=ProviderError(ProviderErrorCode.TIMEOUT, "down"))
    svc = service(provider, ttl_seconds=60)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for fut in [pool.submit(svc.get_quote, s, "stock") for s in ("A", "B", "C")]:
            with pytest.raises(ProviderError):
                fut.result(timeout=10)
    assert svc._inflight == {}
