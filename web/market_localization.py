"""Stable Chinese display names for market metadata.

Providers generally return exchange codes and English security names.  Keep the
mapping in one backend module so every client receives the same terminology;
unknown values intentionally fall back to the provider value.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, TypeVar

import requests


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
    "600418.SS": "江淮汽车",
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


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tencent quote lookup — auto-resolve Chinese asset names for A-shares / HK
# ---------------------------------------------------------------------------

_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={key}"
_TENCENT_TIMEOUT_SECONDS = 2.5
_TENCENT_TTL_SECONDS = 6 * 3600  # 6 hours; names rarely change
_TENCENT_MAX_CACHE = 4096

_tencent_cache: dict[str, tuple[float, str | None]] = {}
_tencent_cache_lock = threading.Lock()

_T = TypeVar("_T")


def _with_cache(key: str, loader: Callable[[], str | None]) -> str | None:
    """Memoize loader() with TTL; safe for concurrent callers."""
    now = time.monotonic()
    with _tencent_cache_lock:
        cached = _tencent_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    value = loader()
    with _tencent_cache_lock:
        if len(_tencent_cache) >= _TENCENT_MAX_CACHE:
            _tencent_cache.pop(next(iter(_tencent_cache)))
        _tencent_cache[key] = (now + _TENCENT_TTL_SECONDS, value)
    return value


def _tencent_symbol_key(symbol: str) -> str | None:
    """Map an internal symbol (e.g. '600418.SS', '00700.HK') to a Tencent key."""
    if not symbol:
        return None
    raw = str(symbol).strip().upper()
    if not raw:
        return None
    raw_lc = raw.lower()
    if raw_lc.startswith(("sh", "sz", "hk")) and len(raw_lc) >= 4:
        return raw_lc
    if "." in raw:
        base, suffix = raw.rsplit(".", 1)
        if suffix in {"SS", "SH"}:
            return f"sh{base}"
        if suffix == "SZ":
            return f"sz{base}"
        if suffix in HK_SUFFIXES:
            # Tencent expects the full 5-digit HK code (e.g. hk00700)
            return f"hk{base.zfill(5)}"
        # Unrecognised suffix (e.g. US crypto) — Tencent does not cover it.
        return None
    if raw.isdigit() and len(raw) == 6:
        # Bare 6-digit codes default to Shanghai when ambiguous
        return f"sh{raw}"
    if raw.isdigit() and len(raw) == 5:
        # Bare 5-digit numeric codes are treated as HK tickers (e.g. 00700)
        return f"hk{raw}"
    return None


HK_SUFFIXES = {"HK", "HKG"}


def _decode_gb18030(data: bytes) -> str:
    for encoding in ("gb18030", "gbk", "gb2312"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _tencent_lookup(key: str) -> str | None:
    """Fetch a single symbol from qt.gtimg.cn and return the Chinese name."""
    try:
        resp = requests.get(
            _TENCENT_QUOTE_URL.format(key=key),
            timeout=_TENCENT_TIMEOUT_SECONDS,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — best-effort lookup
        logger.debug("tencent quote lookup failed for %s: %s", key, exc)
        return None
    text = _decode_gb18030(resp.content)
    # Format: v_sh600418="1~江淮汽车~600418~20.48~..."  → field index 1 is name
    if "=" not in text:
        return None
    payload = text.split("=", 1)[1].strip().strip(';').strip('"')
    parts = payload.split("~")
    if len(parts) < 2:
        return None
    name = parts[1].strip()
    if not name or not _CJK_RE.search(name):
        return None
    return name


def tencent_asset_name(symbol: str | None) -> str | None:
    """Resolve a Chinese asset name via Tencent quote service (with cache)."""
    if not symbol:
        return None
    key = _tencent_symbol_key(symbol)
    if not key:
        return None
    return _with_cache(f"tencent:{key}", lambda: _tencent_lookup(key))

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
    # Last-resort: try Tencent quote service to fetch an authoritative Chinese name.
    remote_zh = tencent_asset_name(symbol)
    if remote_zh:
        return remote_zh
    return clean_english_name(source_name)
