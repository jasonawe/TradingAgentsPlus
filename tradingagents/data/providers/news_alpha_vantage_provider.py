"""AlphaVantage-backed news provider (W3-D2 E2).

Wraps ``tradingagents.dataflows.alpha_vantage_news.get_news``. AlphaVantage
returns either a ``dict[str, str]`` mapping titles to summaries or an
error string. We translate the dict shape into structured articles and
fall back to a stub window on any error.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from .news_base import NewsProvider
from .news_schema import NewsArticle, NewsWindow

LOGGER = logging.getLogger(__name__)


class AlphaVantageNewsProvider(NewsProvider):
    name = "alpha_vantage"

    def supports(self, symbol: str, asset_type: str) -> bool:
        if not symbol:
            return False
        # AlphaVantage supports stock tickers; funds / crypto coverage is
        # spotty, so be conservative.
        return asset_type in ("stock",)

    def get_news(
        self, symbol: str, *, days: int = 7, asset_type: str = "stock",
    ) -> NewsWindow:
        try:
            from tradingagents.dataflows.alpha_vantage_news import get_news
        except Exception as exc:  # pragma: no cover - import error path
            LOGGER.warning("alpha_vantage news import failed: %s", exc)
            win = NewsWindow.stub(symbol)
            win.provider = self.name
            win.warnings.append(f"alpha_vantage unavailable: {exc}")
            return win

        from datetime import timedelta
        end_date = datetime.now(timezone.utc).date().isoformat()
        start_date = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()

        try:
            raw = get_news(ticker=symbol, start_date=start_date, end_date=end_date)
        except Exception as exc:
            LOGGER.warning("alpha_vantage news fetch failed for %s: %s", symbol, exc)
            win = NewsWindow.stub(symbol)
            win.provider = self.name
            win.warnings.append(f"alpha_vantage fetch error: {exc}")
            return win

        items = _parse_alpha_vantage(raw)
        if not items:
            return NewsWindow(symbol=symbol, items=[], provider=self.name)
        return NewsWindow(symbol=symbol, items=items, provider=self.name)

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "status": "configured"}


def _parse_alpha_vantage(raw: Any) -> list[NewsArticle]:
    """AlphaVantage returns ``{title: summary}`` (or error string)."""
    if not isinstance(raw, dict):
        return []
    return [
        NewsArticle(
            title=str(title),
            url="",
            published_at=datetime.now(timezone.utc),
            source="alpha_vantage",
            summary=str(summary) if summary else None,
        )
        for title, summary in raw.items()
    ]
