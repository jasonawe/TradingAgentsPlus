"""Unified data-response container (N93 fix).

Wraps every provider call in a ``DataResponse[T]`` so downstream code can
inspect ``provider``, ``fetched_at`` and ``warnings`` without leaking
provider-specific exceptions. ``chart`` defaults to ``None`` because
chart payloads are rendered client-side; the server MUST NOT pre-render.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class DataResponse(BaseModel, Generic[T]):
    """Standard envelope returned by every provider call.

    Fields
    ------
    results:
        Provider-specific payload (``QuoteSnapshot`` / ``list[Candle]`` /
        ``AssetIdentity`` / …). Use ``DataResponse[QuoteSnapshot]`` at the
        call site so type checkers keep narrow inference.
    provider:
        The :class:`Provider.name` that produced the payload. Required so
        callers can attribute stale data and the cache layer can build
        keys.
    fetched_at:
        UTC timestamp the payload was retrieved. Defaults to ``now()``
        but providers should override for upstream precision.
    warnings:
        Non-fatal issues (e.g. "missing turnover rate"). The route layer
        surfaces these on ``/api/quotes`` responses.
    chart:
        Optional pre-rendered chart payload. Defaults to ``None``; the
        web app renders charts client-side from ``results`` instead.
    """

    results: T
    provider: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    warnings: List[str] = Field(default_factory=list)
    chart: Optional[dict] = None

    model_config = {"arbitrary_types_allowed": True}
