"""Stable Chinese display names for market metadata.

Providers generally return exchange codes and English security names.  Keep the
mapping in one backend module so every client receives the same terminology;
unknown values intentionally fall back to the provider value.
"""

from __future__ import annotations

import re

EXCHANGE_NAMES_ZH: dict[str, str] = {
    "SHH": "上海证券交易所",
    "SHZ": "深圳证券交易所",
    "NMS": "纳斯达克全球精选市场",
    "NGM": "纳斯达克全球市场",
    "NAS": "纳斯达克证券市场",
    "NYQ": "纽约证券交易所",
    "PCX": "纽约证券交易所 Arca 市场",
    "BTT": "纳斯达克交易市场",
    "HKG": "香港交易所",
    "JPX": "东京证券交易所",
    "LSE": "伦敦证券交易所",
    "TOR": "多伦多证券交易所",
    "ASX": "澳大利亚证券交易所",
    "FRA": "法兰克福证券交易所",
    "BSE": "孟买证券交易所",
    "NSI": "印度国家证券交易所",
    "TAI": "台湾证券交易所",
    "CCC": "加密货币市场",
}

ASSET_NAMES_ZH: dict[str, str] = {
    "AAPL": "苹果",
    "MSFT": "微软",
    "NVDA": "英伟达",
    "TSLA": "特斯拉",
    "AMZN": "亚马逊",
    "GOOGL": "谷歌",
    "GOOG": "谷歌",
    "META": "Meta",
    "BTC-USD": "比特币",
    "ETH-USD": "以太坊",
    "513880.SS": "华安三菱日联日经225ETF",
    "688825.SS": "长鑫科技",
    "688836.SS": "昱舒科技",
    "600031.SS": "三一重工",
    "600999.SS": "招商证券",
    "600519.SS": "贵州茅台",
    "600036.SS": "招商银行",
    "601318.SS": "中国平安",
    "000001.SZ": "平安银行",
    "000002.SZ": "万科A",
    "000858.SZ": "五粮液",
    "300750.SZ": "宁德时代",
}

_SOURCE_NAMES_ZH: dict[str, str] = {
    "hua an fund management co., ltd-huaan mitsubishi ufj nikkei 225 etf": "华安三菱日联日经225ETF",
    "cxmt corporation": "长鑫科技",
    "china merchants securities co., ltd.": "招商证券",
}

_EN_NAME_TRAILING = re.compile(
    r"\s*,?\s*(?:co\.?[,\.]?\s*ltd\.?|corporation|inc\.?|company limited|company|holdings?)\s*\.?$",
    re.IGNORECASE,
)


def clean_english_name(value: str | None) -> str | None:
    """Strip corporate suffixes like 'Co.,Ltd' so the watchlist stays compact."""
    if not value:
        return value
    trimmed = _EN_NAME_TRAILING.sub("", str(value).strip())
    return trimmed or value

_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def localized_exchange_name(exchange: str | None) -> str | None:
    """Return a Chinese exchange name when this provider code is known."""

    if not exchange:
        return None
    value = str(exchange).strip()
    return EXCHANGE_NAMES_ZH.get(value.upper(), value)


def exchange_label_key(exchange: str | None) -> str | None:
    """Return a stable UI key only when the exchange code is recognized."""

    if not exchange:
        return None
    value = str(exchange).strip().upper()
    if value not in EXCHANGE_NAMES_ZH:
        return None
    aliases = {
        "NMS": "nasdaq",
        "NGM": "nasdaq",
        "NAS": "nasdaq",
        "NYQ": "nyse",
        "PCX": "nyse",
        "BTT": "nasdaq",
    }
    return f"exchange.{aliases.get(value, value.lower())}"


def localized_asset_name(symbol: str | None, source_name: str | None = None) -> str | None:
    """Prefer an existing Chinese provider name, then a curated symbol map."""

    if source_name and _CJK_RE.search(str(source_name)):
        return str(source_name).strip()
    if source_name:
        normalized = " ".join(str(source_name).strip().split()).casefold()
        if normalized in _SOURCE_NAMES_ZH:
            return _SOURCE_NAMES_ZH[normalized]
    if not symbol:
        return None
    curated = ASSET_NAMES_ZH.get(str(symbol).strip().upper())
    if curated:
        return curated
    return clean_english_name(source_name)
