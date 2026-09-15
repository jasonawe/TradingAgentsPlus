"""YFinance-backed news provider (W3-D2 E2).

Thin wrapper around the existing ``tradingagents.dataflows.yfinance_news``
``get_news_yfinance`` fetcher. Translates the markdown-formatted string
back into structured :class:`NewsArticle` records so the LLM tool gets
a stable, machine-readable shape.

Failures degrade to a stub window (with a warning) so the harness tool
never crashes because the upstream is flaky.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .news_base import NewsProvider
from .news_schema import NewsArticle, NewsWindow

LOGGER = logging.getLogger(__name__)


_LINE_RE = re.compile(
    r"^### (?P<title>.+?)\s*\(source:\s*(?P<source>[^)]+)\)\s*$"
)


class YFinanceNewsProvider(NewsProvider):
    name = "yfinance"

    def supports(self, symbol: str, asset_type: str) -> bool:
        # yfinance can serve most public equities + crypto tickers.
        if not symbol:
            return False
        if asset_type not in ("stock", "crypto", "fund"):
            return False
        return True

    def get_news(
        self, symbol: str, *, days: int = 7, asset_type: str = "stock",
    ) -> NewsWindow:
        try:
            from tradingagents.dataflows.yfinance_news import get_news_yfinance
        except Exception as exc:  # pragma: no cover - import error path
            LOGGER.warning("yfinance news import failed: %s", exc)
            win = NewsWindow.stub(symbol)
            win.provider = self.name
            win.warnings.append(f"yfinance unavailable: {exc}")
            return win

        end_date = datetime.now(timezone.utc).date().isoformat()
        from datetime import timedelta
        start_date = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()

        try:
            raw = get_news_yfinance(ticker=symbol, start_date=start_date, end_date=end_date)
        except Exception as exc:
            LOGGER.warning("yfinance news fetch failed for %s: %s", symbol, exc)
            win = NewsWindow.stub(symbol)
            win.provider = self.name
            win.warnings.append(f"yfinance fetch error: {exc}")
            return win

        items = _parse_yfinance_markdown(raw, symbol)
        if not items:
            # Upstream returned "no news" — keep the empty window, don't
            # fall back to stub (so the LLM can tell the difference
            # between "no news today" and "stub placeholder").
            return NewsWindow(symbol=symbol, items=[], provider=self.name)

        return NewsWindow(symbol=symbol, items=items, provider=self.name)

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "status": "configured"}


def _parse_yfinance_markdown(raw: str, symbol: str) -> list[NewsArticle]:
    """Extract ``### TITLE (source: SRC)`` articles from yfinance markdown."""
    if not isinstance(raw, str) or raw.startswith("No news found") or raw.startswith("Error"):
        return []
    items: list[NewsArticle] = []
    for line in raw.splitlines():
        m = _LINE_RE.match(line.strip())
        if m:
            items.append(
                NewsArticle(
                    title=m.group("title").strip(),
                    url="",  # yfinance markdown drops the link; left for caller
                    published_at=datetime.now(timezone.utc),  # upstream doesn't echo
                    source=m.group("source").strip(),
                )
            )
    return items
