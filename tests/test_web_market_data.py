from datetime import datetime, timedelta, timezone

import pytest

from web.market_data import ProviderRouter, QuoteService
from web.market_models import (
    AssetIdentity,
    Candle,
    QuoteSnapshot,
    ProviderError,
    ProviderErrorCode,
)


def quote(symbol="AAPL", *, fetched_at=None, freshness="fresh", price=100.0, source="test"):
    now = fetched_at or datetime.now(timezone.utc)
    return QuoteSnapshot(symbol=symbol, asset_type="stock", price=price,
                         previous_close=99.0, change=1.0, change_percent=1.01,
                         currency="USD", as_of=now, fetched_at=now,
                         freshness=freshness, source=source)


def test_quote_models_normalize_utc_and_unavailable_numeric_fields():
    value = QuoteSnapshot(symbol="aapl", asset_type="stock", price=None,
                          freshness="unavailable", is_delayed=True,
                          as_of="2026-08-27T12:00:00+08:00",
                          fetched_at="2026-08-27T04:00:00Z")
    assert value.symbol == "AAPL"
    assert value.as_of.tzinfo == timezone.utc
    assert value.price is None and value.change is None
    assert value.is_delayed is True
    assert value.model_dump(mode="json")["as_of"].endswith("Z")


def test_provider_router_only_falls_back_for_transient_typed_errors():
    class P:
        def __init__(self, error=None): self.error = error
        def supports(self, symbol, asset_type, capability): return True
        def get_quote(self, symbol, asset_type):
            if self.error: raise self.error
            return quote(symbol, source="second")
        def get_candles(self, symbol, interval, start, end): return []
        def get_identity(self, symbol, asset_type): return AssetIdentity(symbol=symbol, asset_type=asset_type, name=symbol)

    router = ProviderRouter({"x": P(ProviderError(ProviderErrorCode.TIMEOUT, "down")), "y": P()})
    assert router.get_quote("AAPL", "stock", "x,y").source == "second"
    stopped = ProviderRouter({"x": P(ProviderError(ProviderErrorCode.NO_DATA, "none")), "y": P()})
    with pytest.raises(ProviderError) as exc:
        stopped.get_quote("AAPL", "stock", "x,y")
    assert exc.value.code is ProviderErrorCode.NO_DATA


def test_quote_service_uses_cache_until_ttl_then_marks_stale(tmp_path):
    from web.repositories import QuoteRepository
    from web.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "db.sqlite")
    repo = QuoteRepository(store)
    now = [datetime(2026, 8, 27, 12, tzinfo=timezone.utc)]
    class Provider:
        failed = False
        def supports(self, *args): return True
        def get_quote(self, symbol, asset_type): return quote(symbol, fetched_at=now[0], source="provider")
        def get_candles(self, *args): return []
        def get_identity(self, symbol, asset_type): return AssetIdentity(symbol=symbol, asset_type=asset_type, name=symbol)
    service = QuoteService(ProviderRouter({"yfinance": Provider()}), repo, clock=lambda: now[0], ttl_seconds=60)
    assert service.get_quote("AAPL", "stock").source == "provider"
    now[0] += timedelta(seconds=30)
    assert service.get_quote("AAPL", "stock").source == "cache"
    now[0] += timedelta(seconds=40)
    Provider.get_quote = lambda self, symbol, asset_type: (_ for _ in ()).throw(ProviderError(ProviderErrorCode.TIMEOUT, "offline"))
    stale = service.get_quote("AAPL", "stock")
    assert stale.freshness == "stale"


def test_bulk_limits_and_partial_errors(tmp_path):
    from web.repositories import QuoteRepository
    from web.storage import SQLiteStore
    store = SQLiteStore(tmp_path / "db.sqlite")
    class Provider:
        def supports(self, *args): return True
        def get_quote(self, symbol, asset_type):
            if symbol == "BAD": raise ProviderError(ProviderErrorCode.INVALID_SYMBOL, "bad")
            return quote(symbol)
        def get_candles(self, *args): return []
        def get_identity(self, symbol, asset_type): return AssetIdentity(symbol=symbol, asset_type=asset_type, name=symbol)
    service = QuoteService(ProviderRouter({"yfinance": Provider()}), QuoteRepository(store))
    with pytest.raises(ValueError): service.get_quotes(["AAPL"] * 51)
    result = service.get_quotes(["AAPL", "BAD"])
    assert result.items[0].quote.symbol == "AAPL"
    assert result.items[1].error.code == "invalid_symbol"
