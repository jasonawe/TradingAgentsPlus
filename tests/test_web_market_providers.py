from web.market_models import AssetIdentity, ProviderError, ProviderErrorCode
from web.providers.alpha_vantage_provider import AlphaVantageProvider
from web.providers.yfinance_provider import YFinanceProvider


def test_yfinance_provider_maps_empty_history_to_no_data(monkeypatch):
    class Ticker:
        def history(self, **kwargs):
            import pandas as pd

            return pd.DataFrame()

    monkeypatch.setattr("web.providers.yfinance_provider.yf.Ticker", lambda symbol: Ticker())
    provider = YFinanceProvider()
    try:
        provider.get_quote("AAPL", "stock")
    except ProviderError as exc:
        assert exc.code is ProviderErrorCode.NO_DATA
    else:
        raise AssertionError("expected no_data")


def test_alpha_vantage_missing_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    provider = AlphaVantageProvider(api_key=None)
    assert provider.supports("AAPL", "stock", "quote") is False
    try:
        provider.get_quote("AAPL", "stock")
    except ProviderError as exc:
        assert exc.code is ProviderErrorCode.NOT_CONFIGURED
    else:
        raise AssertionError("expected not_configured")
    assert provider.supports("AAPL", "stock", "candles") is False


def test_provider_errors_redact_secrets():
    err = ProviderError(
        ProviderErrorCode.PROVIDER_ERROR,
        "https://api.test/v1?apikey=secret headers={'Authorization':'Bearer token'}",
    )
    assert "secret" not in str(err) and "token" not in str(err)
    assert "REDACTED" in str(err)
    assert "https://" not in err.message and "headers" not in err.message.lower()


def test_quote_snapshot_exposes_market_fields_and_identity_validates_asset_type():
    from datetime import datetime, timezone

    from pydantic import ValidationError

    value = __import__("web.market_models", fromlist=["QuoteSnapshot"]).QuoteSnapshot(
        symbol="AAPL",
        asset_type="stock",
        fetched_at=datetime.now(timezone.utc),
        open=99,
        high=101,
        low=98,
        volume=1000,
        market_status="open",
        exchange="NASDAQ",
    )
    assert value.open == 99 and value.market_status == "open"
    try:
        AssetIdentity(symbol="AAPL", asset_type="bond")
    except ValidationError:
        pass
    else:
        raise AssertionError("invalid asset type accepted")


def test_alpha_injected_key_does_not_mutate_environment(monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    provider = AlphaVantageProvider(api_key="injected")
    seen = []
    monkeypatch.setattr(
        "web.providers.alpha_vantage_provider._make_api_request",
        lambda fn, params, **kwargs: (
            seen.append((params, kwargs.get("api_key")))
            or {"Global Quote": {"05. price": "1", "07. latest trading day": "2026-08-27"}}
        ),
    )
    provider.get_quote("AAPL", "stock")
    assert "ALPHA_VANTAGE_API_KEY" not in __import__("os").environ
    assert seen and seen[0][1] == "injected"


def test_yfinance_quote_maps_ohlcv_and_metadata(monkeypatch):
    import pandas as pd

    class Ticker:
        info = {"exchange": "NASDAQ", "currency": "USD", "longName": "Apple Inc."}

        def history(self, **kwargs):
            return pd.DataFrame(
                {"Open": [99], "High": [101], "Low": [98], "Close": [100], "Volume": [1234]},
                index=pd.to_datetime(["2026-08-27"]),
            )

    monkeypatch.setattr("web.providers.yfinance_provider.yf.Ticker", lambda symbol: Ticker())
    value = YFinanceProvider().get_quote("AAPL", "stock")
    assert (value.open, value.high, value.low, value.volume) == (99, 101, 98, 1234)
    assert value.exchange == "NASDAQ" and value.currency == "USD"
    assert "Apple" in (value.raw_summary or "")


def test_alpha_instances_pass_isolated_keys_to_helper(monkeypatch):
    import threading

    seen = []
    barrier = threading.Barrier(2)

    def fake(function, params, *, api_key=None):
        seen.append(api_key)
        barrier.wait(timeout=1)
        return {"Global Quote": {"05. price": "1", "07. latest trading day": "2026-08-27"}}

    monkeypatch.setattr("web.providers.alpha_vantage_provider._make_api_request", fake)
    a, b = AlphaVantageProvider(api_key="key-a"), AlphaVantageProvider(api_key="key-b")
    threads = [
        threading.Thread(target=lambda p: p.get_quote("AAPL", "stock"), args=(p,)) for p in (a, b)
    ]
    [t.start() for t in threads]
    [t.join(timeout=2) for t in threads]
    assert sorted(seen) == ["key-a", "key-b"]
