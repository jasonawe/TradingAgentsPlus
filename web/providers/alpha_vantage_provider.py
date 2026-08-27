from __future__ import annotations

import os
from datetime import datetime, timezone

from cli.utils import is_valid_ticker_input, normalize_ticker_symbol
from tradingagents.dataflows.alpha_vantage_common import _make_api_request

from ..market_models import (
    AssetIdentity,
    Candle,
    Freshness,
    ProviderError,
    ProviderErrorCode,
    QuoteSnapshot,
)


class AlphaVantageProvider:
    name = "alpha_vantage"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("ALPHA_VANTAGE_API_KEY")

    def supports(self, symbol: str, asset_type: str, capability: str) -> bool:
        return bool(self.api_key) and asset_type == "stock" and capability in {"quote", "identity"}

    def _check(self, symbol: str):
        if not is_valid_ticker_input(symbol):
            raise ProviderError(ProviderErrorCode.INVALID_SYMBOL, "invalid symbol")
        if not self.api_key:
            raise ProviderError(ProviderErrorCode.NOT_CONFIGURED, "API key is not configured")

    def get_quote(self, symbol: str, asset_type: str) -> QuoteSnapshot:
        self._check(symbol)
        try:
            raw = _make_api_request(
                "GLOBAL_QUOTE", {"symbol": normalize_ticker_symbol(symbol)}, api_key=self.api_key
            )
        except Exception as exc:
            text = str(exc).lower()
            code = (
                ProviderErrorCode.RATE_LIMITED
                if "rate limit" in text or "requests per day" in text
                else ProviderErrorCode.PROVIDER_ERROR
            )
            raise ProviderError(code, str(exc)) from exc
        if not isinstance(raw, dict):
            import json

            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        data = raw.get("Global Quote") or raw.get("global_quote") or raw
        if not data or not data.get("05. price"):
            raise ProviderError(ProviderErrorCode.NO_DATA, "no quote data")
        price = float(data["05. price"])
        previous = float(data.get("08. previous close") or 0) or None
        change = float(data.get("09. change") or 0) if previous is not None else None
        trading_day = data.get("07. latest trading day")
        try:
            as_of = (
                datetime.fromisoformat(str(trading_day)).replace(tzinfo=timezone.utc)
                if trading_day
                else datetime.now(timezone.utc)
            )
        except ValueError:
            as_of = datetime.now(timezone.utc)
        return QuoteSnapshot(
            symbol=normalize_ticker_symbol(symbol),
            asset_type=asset_type,
            price=price,
            previous_close=previous,
            change=change,
            change_percent=float(str(data.get("10. change percent", "0")).strip("%"))
            if data.get("10. change percent")
            else None,
            open=float(data.get("02. open")) if data.get("02. open") else None,
            high=float(data.get("03. high")) if data.get("03. high") else None,
            low=float(data.get("04. low")) if data.get("04. low") else None,
            volume=float(data.get("06. volume")) if data.get("06. volume") else None,
            as_of=as_of,
            fetched_at=datetime.now(timezone.utc),
            freshness=Freshness.DELAYED,
            is_delayed=True,
            source=self.name,
        )

    def get_candles(self, symbol: str, interval: str, start, end) -> list[Candle]:
        self._check(symbol)
        raise ProviderError(ProviderErrorCode.NO_DATA, "historical candles unavailable")

    def get_identity(self, symbol: str, asset_type: str) -> AssetIdentity:
        self._check(symbol)
        return AssetIdentity(symbol=normalize_ticker_symbol(symbol), asset_type=asset_type)
