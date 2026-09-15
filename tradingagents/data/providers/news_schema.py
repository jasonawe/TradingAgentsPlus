"""Shared news schema for the news_provider seam (W3-D2 E2).

Kept tiny on purpose — only what the LLM tool surfaces back to the
synthesizer. Concrete providers translate their upstream article shape
into ``NewsArticle`` so the harness tool contract is stable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class NewsArticle(BaseModel):
    title: str
    url: str = ""
    published_at: datetime
    source: Optional[str] = None
    summary: Optional[str] = None
    sentiment: Optional[float] = None


class NewsWindow(BaseModel):
    symbol: str
    items: list[NewsArticle] = Field(default_factory=list)
    provider: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def stub(cls, symbol: str) -> "NewsWindow":
        """Build a single-item stub window (used by StubNewsProvider)."""
        return cls(
            symbol=symbol,
            items=[
                NewsArticle(
                    title=f"[stub] news for {symbol}",
                    url="about:blank",
                    published_at=datetime.now(timezone.utc),
                    sentiment=0.0,
                )
            ],
            provider="stub",
            warnings=["stub provider — no real upstream data fetched"],
        )
