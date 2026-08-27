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


def test_provider_errors_redact_secrets():
    err = ProviderError(ProviderErrorCode.PROVIDER_ERROR, "https://api.test/v1?apikey=secret headers={'Authorization':'Bearer token'}")
    assert "secret" not in str(err) and "token" not in str(err)
    assert "REDACTED" in str(err)
