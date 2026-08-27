from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


_SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|authorization|password|secret)(\s*[=:]\s*)([\"']?)([^\"'\s,}&]+)([\"']?)")
_URL_QUERY_RE = re.compile(r"([?&](?:api[_-]?key|token|key|secret)=)([^&\s]+)", re.I)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[^\s'\"},]+")


def redact(value: Any) -> str:
    text = str(value)
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
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    currency: str | None = None
    as_of: datetime | None = None
    fetched_at: datetime
    freshness: Freshness = Freshness.FRESH
    is_delayed: bool = False
    source: str | None = None
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
            self.is_delayed = True
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
    def normalize_symbol(cls, value: str) -> str: return str(value).strip().upper()

    @field_validator("timestamp", mode="before")
    @classmethod
    def normalize_timestamp(cls, value): return _utc(value)


class AssetIdentity(BaseModel):
    symbol: str
    asset_type: str
    name: str | None = None
    exchange: str | None = None
    currency: str | None = None


class QuoteItemError(BaseModel):
    symbol: str
    code: str
    message: str


class QuoteItem(BaseModel):
    symbol: str
    quote: QuoteSnapshot | None = None
    error: QuoteItemError | None = None


class BulkQuoteResponse(BaseModel):
    items: list[QuoteItem]
    source: str | None = None


class ProviderDiagnostic(BaseModel):
    id: str
    available: bool
    configured: bool
    capabilities: list[str] = Field(default_factory=list)
    message: str | None = None
