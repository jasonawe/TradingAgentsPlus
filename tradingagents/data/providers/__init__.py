"""Provider package: ABC, concrete adapters and the active-provider registry."""
from .base import Provider
from .akshare_provider import AKShareProvider
from .alpha_vantage_provider import AlphaVantageProvider
from .eastmoney_provider import EastMoneyProvider
from .yfinance_provider import YFinanceProvider

__all__ = [
    "Provider",
    "AKShareProvider",
    "AlphaVantageProvider",
    "EastMoneyProvider",
    "YFinanceProvider",
]
