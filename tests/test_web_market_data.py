import time
from datetime import datetime, timedelta, timezone

import pytest

from web.market_data import ProviderRouter, QuoteService
from web.market_models import (
    AssetIdentity,
    ProviderError,
    ProviderErrorCode,
    QuoteSnapshot,
)


def quote(symbol="AAPL", *, fetched_at=None, freshness="fresh", price=100.0, source="test"):
    now = fetched_at or datetime.now(timezone.utc)
    return QuoteSnapshot(
        symbol=symbol,
        asset_type="stock",
        price=price,
        previous_close=99.0,
        change=1.0,
        change_percent=1.01,
        currency="USD",
        as_of=now,
        fetched_at=now,
        freshness=freshness,
        source=source,
        exchange="NASDAQ",
        raw_summary="Apple Inc.",
    )


def test_quote_models_normalize_utc_and_unavailable_numeric_fields():
    value = QuoteSnapshot(
        symbol="aapl",
        asset_type="stock",
        price=None,
        freshness="unavailable",
        is_delayed=True,
        as_of="2026-08-27T12:00:00+08:00",
        fetched_at="2026-08-27T04:00:00Z",
    )
    assert value.symbol == "AAPL"
    assert value.as_of.tzinfo == timezone.utc
    assert value.price is None and value.change is None
    assert value.is_delayed is False
    assert value.model_dump(mode="json")["as_of"].endswith("Z")


def test_quote_item_flattens_asset_name_and_exchange():
    quote_item = __import__("web.market_models", fromlist=["QuoteItem"]).QuoteItem(
        symbol="AAPL", quote=quote()
    )
    assert quote_item.asset_name == "Apple Inc."
    assert quote_item.exchange == "NASDAQ"


def test_quote_item_exposes_chinese_asset_and_exchange_names():
    quote_item = __import__("web.market_models", fromlist=["QuoteItem"]).QuoteItem(
        symbol="513880.SS",
        quote=QuoteSnapshot(
            symbol="513880.SS",
            asset_type="stock",
            price=2.14,
            fetched_at=datetime.now(timezone.utc),
            exchange="SHH",
            raw_summary="Hua An Fund Management Co., Ltd-HuaAn Mitsubishi UFJ Nikkei 225 ETF",
        ),
    )
    assert quote_item.asset_name_zh == "华安三菱日联日经225ETF"
    assert quote_item.exchange_name_zh == "上海证券交易所"


def test_unknown_localization_keeps_source_values():
    quote_item = __import__("web.market_models", fromlist=["QuoteItem"]).QuoteItem(
        symbol="UNKNOWN.XY",
        quote=QuoteSnapshot(
            symbol="UNKNOWN.XY",
            asset_type="stock",
            price=1.0,
            fetched_at=datetime.now(timezone.utc),
            exchange="XYX",
            raw_summary="Unknown Holdings Ltd",
        ),
    )
    assert quote_item.asset_name_zh is None
    assert quote_item.exchange_name_zh == "XYX"


@pytest.mark.parametrize(
    ("symbol", "source_name", "expected"),
    [
        ("688825.SS", "CXMT Corporation", "长鑫科技"),
        ("600999.SS", "China Merchants Securities Co., Ltd.", "招商证券"),
    ],
)
def test_common_chinese_listed_assets_use_chinese_display_names(symbol, source_name, expected):
    quote_item = __import__("web.market_models", fromlist=["QuoteItem"]).QuoteItem(
        symbol=symbol,
        quote=QuoteSnapshot(
            symbol=symbol,
            asset_type="stock",
            price=1.0,
            fetched_at=datetime.now(timezone.utc),
            exchange="SHH",
            raw_summary=source_name,
        ),
    )
    assert quote_item.asset_name_zh == expected


def test_provider_router_only_falls_back_for_transient_typed_errors():
    class P:
        def __init__(self, error=None):
            self.error = error

        def supports(self, symbol, asset_type, capability):
            return True

        def get_quote(self, symbol, asset_type):
            if self.error:
                raise self.error
            return quote(symbol, source="second")

        def get_candles(self, symbol, interval, start, end):
            return []

        def get_identity(self, symbol, asset_type):
            return AssetIdentity(symbol=symbol, asset_type=asset_type, name=symbol)

    strategies = {"test": ("x", "y")}
    router = ProviderRouter(
        {"x": P(ProviderError(ProviderErrorCode.TIMEOUT, "down")), "y": P()}, strategies=strategies
    )
    assert router.get_quote("AAPL", "stock", "test").source == "second"
    stopped = ProviderRouter(
        {"x": P(ProviderError(ProviderErrorCode.NO_DATA, "none")), "y": P()}, strategies=strategies
    )
    with pytest.raises(ProviderError) as exc:
        stopped.get_quote("AAPL", "stock", "test")
    assert exc.value.code is ProviderErrorCode.NO_DATA


def test_quote_service_uses_cache_until_ttl_then_marks_stale(tmp_path):
    from web.repositories import QuoteRepository
    from web.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "db.sqlite")
    repo = QuoteRepository(store)
    now = [datetime(2026, 8, 27, 12, tzinfo=timezone.utc)]

    class Provider:
        failed = False

        def supports(self, *args):
            return True

        def get_quote(self, symbol, asset_type):
            return quote(symbol, fetched_at=now[0], source="provider")

        def get_candles(self, *args):
            return []

        def get_identity(self, symbol, asset_type):
            return AssetIdentity(symbol=symbol, asset_type=asset_type, name=symbol)

    service = QuoteService(
        ProviderRouter({"yfinance": Provider()}), repo, clock=lambda: now[0], ttl_seconds=60
    )
    assert service.get_quote("AAPL", "stock").source == "provider"
    now[0] += timedelta(seconds=30)
    assert service.get_quote("AAPL", "stock").cache_status == "hit"
    now[0] += timedelta(seconds=40)
    Provider.get_quote = lambda self, symbol, asset_type: (_ for _ in ()).throw(
        ProviderError(ProviderErrorCode.TIMEOUT, "offline")
    )
    stale = service.get_quote("AAPL", "stock")
    assert stale.freshness == "stale"


def test_bulk_limits_and_partial_errors(tmp_path):
    from web.repositories import QuoteRepository
    from web.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "db.sqlite")

    class Provider:
        def supports(self, *args):
            return True

        def get_quote(self, symbol, asset_type):
            if symbol == "BAD":
                raise ProviderError(ProviderErrorCode.INVALID_SYMBOL, "bad")
            return quote(symbol)

        def get_candles(self, *args):
            return []

        def get_identity(self, symbol, asset_type):
            return AssetIdentity(symbol=symbol, asset_type=asset_type, name=symbol)

    service = QuoteService(ProviderRouter({"yfinance": Provider()}), QuoteRepository(store))
    with pytest.raises(ValueError):
        service.get_quotes(["AAPL"] * 51)
    result = service.get_quotes(["AAPL", "BAD"])
    assert result.items[0].quote.symbol == "AAPL"
    assert result.items[1].error.code == "invalid_symbol"
    assert result.partial is True


def test_quote_service_precedence_defaults_then_sqlite_then_environment(monkeypatch):
    class Settings:
        def get(self, key):
            return {
                "value": "120" if key == "quote_ttl_seconds" else "fallback-yfinance-alpha-vantage"
            }

    router = ProviderRouter({"yfinance": object(), "alpha_vantage": object()})
    monkeypatch.setenv("TRADINGAGENTS_QUOTE_TTL_SECONDS", "30")
    monkeypatch.setenv("TRADINGAGENTS_QUOTE_STRATEGY", "default-yfinance")
    service = QuoteService(
        router,
        object(),
        settings=Settings(),
        config={"quote_ttl_seconds": 15, "quote_strategy_id": "default-yfinance"},
    )
    assert service.ttl_seconds == 30
    assert service.strategy == "default-yfinance"


def test_router_checks_capability_before_calling_provider():
    class P:
        def supports(self, symbol, asset_type, capability):
            return capability != "candles"

        def get_quote(self, *args):
            raise AssertionError("must not call unsupported provider")

        def get_candles(self, *args):
            raise AssertionError("must not call unsupported provider")

        def get_identity(self, *args):
            raise AssertionError("must not call unsupported provider")

    router = ProviderRouter({"p": P()}, strategies={"s": ("p",)})
    with pytest.raises(ProviderError) as exc:
        router.get_candles("AAPL", "1d", "2026-01-01", "2026-01-02", "s")
    assert exc.value.code is ProviderErrorCode.NOT_CONFIGURED


def test_router_retries_transient_provider_error_with_bound(monkeypatch):
    class P:
        def __init__(self):
            self.calls = 0

        def supports(self, *args):
            return True

        def get_quote(self, symbol, asset_type):
            self.calls += 1
            if self.calls < 2:
                raise ProviderError(ProviderErrorCode.TIMEOUT, "temporary")
            return quote(symbol, source="retry")

        def get_candles(self, *args):
            return []

        def get_identity(self, *args):
            return AssetIdentity(symbol="AAPL", asset_type="stock")

    provider = P()
    result = ProviderRouter({"p": provider}, strategies={"s": ("p",)}).get_quote(
        "AAPL", "stock", "s", retries=1
    )
    assert result.source == "retry" and provider.calls == 2


def test_router_does_not_retry_or_fallback_for_terminal_provider_errors():
    class P:
        def __init__(self, code):
            self.code = code
            self.calls = 0
        def supports(self, *args): return True
        def get_quote(self, *args):
            self.calls += 1
            raise ProviderError(self.code, "terminal")
        def get_candles(self, *args): return []
        def get_identity(self, *args): return AssetIdentity(symbol="AAPL", asset_type="stock")
    for code in (ProviderErrorCode.INVALID_SYMBOL, ProviderErrorCode.NO_DATA):
        first, second = P(code), P(code)
        router = ProviderRouter({"first": first, "second": second}, strategies={"s": ("first", "second")})
        with pytest.raises(ProviderError):
            router.get_quote("AAPL", "stock", "s", retries=3)
        assert first.calls == 1 and second.calls == 0


def test_router_timeout_does_not_wait_for_hung_worker():
    class P:
        def supports(self, *args):
            return True

        def get_quote(self, *args):
            time.sleep(0.25)
            return quote("AAPL")

        def get_candles(self, *args):
            time.sleep(0.25)
            return []

        def get_identity(self, *args):
            time.sleep(0.25)
            return AssetIdentity(symbol="AAPL", asset_type="stock")

    router = ProviderRouter({"p": P()}, strategies={"s": ("p",)})
    started = time.monotonic()
    with pytest.raises(ProviderError):
        router.get_quote("AAPL", "stock", "s", timeout_seconds=0.01)
    assert time.monotonic() - started < 0.1


def test_unavailable_bulk_item_contains_null_quote_snapshot(tmp_path):
    from web.repositories import QuoteRepository
    from web.storage import SQLiteStore

    class P:
        def supports(self, *args):
            return True

        def get_quote(self, *args):
            raise ProviderError(ProviderErrorCode.PROVIDER_ERROR, "down")

        def get_candles(self, *args):
            return []

        def get_identity(self, *args):
            return AssetIdentity(symbol="AAPL", asset_type="stock")

    service = QuoteService(
        ProviderRouter({"yfinance": P()}), QuoteRepository(SQLiteStore(tmp_path / "db.sqlite"))
    )
    item = service.get_quotes(["AAPL"]).items[0]
    assert item.quote.freshness == "unavailable" and item.quote.price is None
    assert item.quote.open is None and item.error is not None


def test_malformed_symbol_bulk_item_also_has_unavailable_quote(tmp_path):
    from web.repositories import QuoteRepository
    from web.storage import SQLiteStore

    class P:
        def supports(self, *args):
            return True

        def get_quote(self, *args):
            raise ProviderError(ProviderErrorCode.INVALID_SYMBOL, "bad")

        def get_candles(self, *args):
            return []

        def get_identity(self, *args):
            return AssetIdentity(symbol="AAPL", asset_type="stock")

    service = QuoteService(
        ProviderRouter({"yfinance": P()}), QuoteRepository(SQLiteStore(tmp_path / "db.sqlite"))
    )
    result = service.get_quotes(["../bad"])
    assert result.partial and result.items[0].quote.freshness == "unavailable"


def test_provider_workers_are_daemon_and_bounded():
    import threading
    workers = [t for t in threading.enumerate() if t.name.startswith("market-provider")]
    assert workers and all(t.daemon for t in workers) and len(workers) <= 8


def test_router_records_health_for_each_actual_fallback_attempt(tmp_path):
    from web.repositories import ProviderHealthRepository
    from web.storage import SQLiteStore

    class P:
        def __init__(self, error=None, source=None):
            self.error = error
            self.source = source

        def supports(self, *args):
            return True

        def get_quote(self, symbol, asset_type):
            if self.error:
                raise self.error
            return quote(symbol, source=self.source)

    health = ProviderHealthRepository(SQLiteStore(tmp_path / "health.sqlite3"))
    router = ProviderRouter(
        {
            "first": P(ProviderError(ProviderErrorCode.TIMEOUT, "slow")),
            "second": P(source="second"),
        },
        strategies={"fallback": ("first", "second")},
        health=health,
    )
    assert router.get_quote("AAPL", "stock", "fallback", retries=0).source == "second"
    assert health.get("first")["failure_count"] == 1
    assert health.get("second")["status"] == "ready"
    assert health.get("second")["request_count"] == 1


def test_quote_freshness_matrix_preserves_cache_timestamps_and_reports_age(tmp_path):
    from web.repositories import QuoteRepository
    from web.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "quotes.sqlite3")
    repo = QuoteRepository(store)
    now = [datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)]

    class Provider:
        fail = False
        delayed = False

        def supports(self, *args):
            return True

        def get_quote(self, symbol, asset_type):
            if self.fail:
                raise ProviderError(ProviderErrorCode.TIMEOUT, "offline")
            return quote(
                symbol,
                fetched_at=now[0],
                freshness="delayed" if self.delayed else "fresh",
                source="provider",
            )

    provider = Provider()
    service = QuoteService(
        ProviderRouter({"yfinance": provider}),
        repo,
        clock=lambda: now[0],
        ttl_seconds=60,
        retries=0,
    )
    live = service.get_quote("AAPL")
    assert live.cache_status == "live" and live.provider_status == "ready"
    original_fetched_at = live.fetched_at
    now[0] += timedelta(seconds=30)
    hit = service.get_quote("AAPL")
    assert hit.cache_status == "hit" and hit.fetched_at == original_fetched_at
    assert hit.stale_seconds == 30
    now[0] += timedelta(seconds=40)
    provider.fail = True
    stale = service.get_quote("AAPL")
    assert stale.cache_status == "hit" and stale.freshness == "stale"
    assert stale.fetched_at == original_fetched_at and stale.stale_seconds == 70
    unavailable = QuoteService(
        ProviderRouter({"yfinance": provider}),
        QuoteRepository(SQLiteStore(tmp_path / "empty.sqlite3")),
        clock=lambda: now[0],
        retries=0,
    ).get_quotes(["MSFT"]).items[0].quote
    assert unavailable.cache_status == "miss"
    assert unavailable.provider_status == "degraded"
    assert unavailable.freshness == "unavailable"


def test_quote_and_identity_models_add_stable_localization_keys():
    snapshot = quote("AAPL", freshness="stale")
    snapshot.cache_status = "hit"
    snapshot.provider_status = "degraded"
    item = __import__("web.market_models", fromlist=["QuoteItem"]).QuoteItem(
        symbol="AAPL", quote=snapshot
    )
    dumped = item.model_dump(mode="json")
    assert dumped["freshness"] == "stale"
    assert dumped["freshness_key"] == "freshness.stale"
    assert dumped["cache_status"] == "hit"
    assert dumped["cache_status_key"] == "cache_status.hit"
    assert dumped["provider_status"] == "degraded"
    assert dumped["provider_status_key"] == "provider_status.degraded"

    identity = AssetIdentity(
        symbol="600999.SS",
        asset_type="stock",
        name="China Merchants Securities Co., Ltd.",
        exchange="SHH",
    ).model_dump(mode="json")
    assert identity["name"] == "China Merchants Securities Co., Ltd."
    assert identity["name_zh"] == "招商证券"
    assert identity["exchange"] == "SHH"
    assert identity["exchange_name_zh"] == "上海证券交易所"
    assert identity["exchange_key"] == "exchange.shh"

    unknown = AssetIdentity(
        symbol="EXAMPLE", asset_type="stock", exchange="XYZ"
    ).model_dump(mode="json")
    assert unknown["exchange"] == "XYZ"
    assert unknown["exchange_key"] is None
