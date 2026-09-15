"""Stub news provider (W3-D2 E2).

Always returns a single ``[stub] news for SYM`` article. Useful for
tests, offline dev, and as the explicit fallback when no upstream
provider is configured. Mirrors the previous in-tool stub behaviour
exactly so the harness tool sees the same response shape.
"""
from __future__ import annotations

from typing import Any, Iterable

from .news_base import NewsProvider
from .news_schema import NewsWindow


class StubNewsProvider(NewsProvider):
    name = "stub"

    def supports(self, symbol: str, asset_type: str) -> bool:
        # Stub supports everything — it's the universal fallback.
        return bool(symbol)

    def get_news(
        self, symbol: str, *, days: int = 7, asset_type: str = "stock",
    ) -> NewsWindow:
        return NewsWindow.stub(symbol)

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "status": "configured"}
