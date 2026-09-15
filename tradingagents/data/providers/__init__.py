"""Provider package: ABC, concrete adapters and the active-provider registry.

Quote providers (E1) live in this directory alongside the news (E2)
and alpha (E3) seams so the settings UI / harness can resolve any
seam through the same import path.
"""
from .base import Provider
from .akshare_provider import AKShareProvider
from .alpha_vantage_provider import AlphaVantageProvider
from .eastmoney_provider import EastMoneyProvider
from .yfinance_provider import YFinanceProvider
from .news_base import NewsProvider
from .news_schema import NewsArticle, NewsWindow
from .news_stub_provider import StubNewsProvider
from .news_yfinance_provider import YFinanceNewsProvider
from .news_alpha_vantage_provider import AlphaVantageNewsProvider
from .alpha_base import AlphaProvider
from .alpha_stub_provider import StubAlphaProvider
from .alpha_yfinance_provider import YFinanceAlphaProvider
from .alpha_akshare_provider import AKShareAlphaProvider

__all__ = [
    # Quote seam (E1)
    "Provider",
    "AKShareProvider",
    "AlphaVantageProvider",
    "EastMoneyProvider",
    "YFinanceProvider",
    # News seam (E2)
    "NewsProvider",
    "NewsArticle",
    "NewsWindow",
    "StubNewsProvider",
    "YFinanceNewsProvider",
    "AlphaVantageNewsProvider",
    # Alpha seam (E3)
    "AlphaProvider",
    "StubAlphaProvider",
    "YFinanceAlphaProvider",
    "AKShareAlphaProvider",
]
