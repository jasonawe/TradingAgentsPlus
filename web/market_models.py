from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .market_localization import (
    exchange_label_key,
    localized_asset_name,
    localized_exchange_name,
)


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Freshness(str, Enum):
    FRESH = "fresh"
    DELAYED = "delayed"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class ProviderErrorCode(str, Enum):
    NOT_CONFIGURED = "not_configured"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NO_DATA = "no_data"
    INVALID_SYMBOL = "invalid_symbol"
    PROVIDER_ERROR = "provider_error"


_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|authorization|password|secret)(\s*[=:]\s*)([\"']?)([^\"'\s,}&]+)([\"']?)"
)
_URL_QUERY_RE = re.compile(r"([?&](?:api[_-]?key|token|key|secret)=)([^&\s]+)", re.I)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[^\s'\"},]+")


def redact(value: Any) -> str:
    text = str(value)
    text = re.sub(r"https?://[^\s'\"}]+", "[URL_REDACTED]", text, flags=re.I)
    text = re.sub(r"(?i)\bheaders?\s*=\s*\{[^}]*\}", "[REDACTED]", text)
    text = _SECRET_RE.sub(r"\1\2\3REDACTED\5", text)
    text = _URL_QUERY_RE.sub(r"\1REDACTED", text)
    text = _BEARER_RE.sub(r"\1REDACTED", text)
    return text[:512]


class ProviderError(Exception):
    def __init__(self, code: ProviderErrorCode | str, message: str = ""):
        self.code = ProviderErrorCode(code)
        self.message = redact(message) or self.code.value
        super().__init__(f"{self.code.value}: {self.message}")


class QuoteSnapshot(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    symbol: str
    asset_type: str = "stock"
    price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    volume_ratio: float | None = None
    turnover: float | None = None
    turnover_rate: float | None = None
    market_cap: float | None = None
    circulating_cap: float | None = None
    pe_ratio: float | None = None
    amplitude: float | None = None
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    currency: str | None = None
    as_of: datetime | None = None
    fetched_at: datetime
    freshness: Freshness = Freshness.FRESH
    is_delayed: bool = False
    source: str | None = None
    market_status: str | None = None
    exchange: str | None = None
    raw_summary: str | None = None
    cache_status: Literal["live", "hit", "miss", "stale"] | None = None
    provider_status: Literal["ready", "degraded", "not_configured", "error"] | None = None
    stale_seconds: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return str(value).strip().upper()

    @field_validator("as_of", "fetched_at", mode="before")
    @classmethod
    def normalize_datetime(cls, value):
        return _utc(value)

    @model_validator(mode="after")
    def enforce_freshness(self):
        if self.freshness == Freshness.FRESH:
            self.is_delayed = False
        elif self.freshness == Freshness.UNAVAILABLE:
            self.price = self.previous_close = self.change = self.change_percent = None
            self.open = self.high = self.low = self.volume = self.turnover = None
            self.volume_ratio = self.turnover_rate = self.market_cap = self.circulating_cap = None
            self.pe_ratio = self.amplitude = None
            self.is_delayed = False
        elif self.freshness in (Freshness.DELAYED, Freshness.STALE):
            self.is_delayed = True
        return self


class Candle(BaseModel):
    symbol: str
    interval: str
    timestamp: datetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    source: str | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return str(value).strip().upper()

    @field_validator("timestamp", mode="before")
    @classmethod
    def normalize_timestamp(cls, value):
        return _utc(value)


class AssetIdentity(BaseModel):
    symbol: str
    asset_type: str
    name: str | None = None
    name_zh: str | None = None
    exchange: str | None = None
    exchange_name_zh: str | None = None
    exchange_key: str | None = None
    currency: str | None = None

    @field_validator("asset_type")
    @classmethod
    def validate_asset_type(cls, value: str) -> str:
        if value not in {"stock", "crypto"}:
            raise ValueError("asset_type must be stock or crypto")
        return value

    @model_validator(mode="after")
    def localize_display_names(self):
        self.name_zh = localized_asset_name(self.symbol, self.name)
        self.exchange_name_zh = localized_exchange_name(self.exchange)
        self.exchange_key = exchange_label_key(self.exchange)
        return self


class QuoteItemError(BaseModel):
    symbol: str
    code: str
    message: str


class QuoteItem(BaseModel):
    symbol: str
    canonical_symbol: str | None = None
    asset_type: str = "stock"
    price: float | None = None
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    currency: str | None = None
    market_status: str | None = None
    exchange: str | None = None
    asset_name: str | None = None
    asset_name_zh: str | None = None
    exchange_name_zh: str | None = None
    source: str | None = None
    volume: float | None = None
    turnover: float | None = None
    turnover_rate: float | None = None
    market_cap: float | None = None
    circulating_cap: float | None = None
    pe_ratio: float | None = None
    amplitude: float | None = None
    volume_ratio: float | None = None
    quote_time: datetime | None = None
    fetched_at: datetime | None = None
    freshness: Freshness | None = None
    freshness_key: str | None = None
    is_delayed: bool = False
    cache_status: str | None = None
    cache_status_key: str | None = None
    provider_status: str | None = None
    provider_status_key: str | None = None
    stale_seconds: int | None = None
    quote: QuoteSnapshot | None = None
    error: QuoteItemError | None = None

    @model_validator(mode="after")
    def flatten_quote(self):
        if self.quote is not None:
            self.canonical_symbol = self.quote.symbol
            self.asset_type = self.quote.asset_type
            self.price = self.quote.price
            self.previous_close = self.quote.previous_close
            self.change = self.quote.change
            self.change_percent = self.quote.change_percent
            self.volume = self.quote.volume
            self.turnover = self.quote.turnover
            self.turnover_rate = self.quote.turnover_rate
            self.market_cap = self.quote.market_cap
            self.circulating_cap = self.quote.circulating_cap
            self.pe_ratio = self.quote.pe_ratio
            self.amplitude = self.quote.amplitude
            self.volume_ratio = self.quote.volume_ratio
            self.currency = self.quote.currency
            self.market_status = self.quote.market_status
            self.exchange = self.quote.exchange
            self.asset_name = self.quote.raw_summary
            self.asset_name_zh = localized_asset_name(self.quote.symbol, self.quote.raw_summary)
            self.exchange_name_zh = localized_exchange_name(self.quote.exchange)
            self.source = self.quote.source
            self.quote_time = self.quote.as_of
            self.fetched_at = self.quote.fetched_at
            self.freshness = self.quote.freshness
            self.is_delayed = self.quote.is_delayed
            self.cache_status = self.quote.cache_status
            self.provider_status = self.quote.provider_status
            self.stale_seconds = self.quote.stale_seconds
        freshness = getattr(self.freshness, "value", self.freshness)
        self.freshness = Freshness(freshness) if freshness else None
        self.freshness_key = f"freshness.{freshness}" if freshness else None
        self.cache_status_key = (
            f"cache_status.{self.cache_status}" if self.cache_status else None
        )
        self.provider_status_key = (
            f"provider_status.{self.provider_status}"
            if self.provider_status
            else None
        )
        return self


class BulkQuoteResponse(BaseModel):
    items: list[QuoteItem]
    partial: bool = False


class QuoteProvider(Protocol):
    def supports(self, symbol: str, asset_type: str, capability: str) -> bool: ...
    def get_quote(self, symbol: str, asset_type: str) -> QuoteSnapshot: ...
    def get_candles(self, symbol: str, interval: str, start, end) -> list[Candle]: ...
    def get_identity(self, symbol: str, asset_type: str) -> AssetIdentity: ...


class ProviderDiagnostic(BaseModel):
    id: str
    available: bool
    configured: bool
    capabilities: list[str] = Field(default_factory=list)
    message: str | None = None
