from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import time
import urllib.error
import urllib.parse
import urllib.request
import json

from ..market_models import (
    AssetIdentity,
    Candle,
    Freshness,
    ProviderError,
    ProviderErrorCode,
    QuoteSnapshot,
)


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _is_a_share(symbol: str) -> bool:
    s = _normalize_symbol(symbol)
    return s.endswith(".SS") or s.endswith(".SZ")


def _secid(symbol: str) -> str:
    """Convert .SS/.SZ ticker to EastMoney secid (market.code)."""
    s = _normalize_symbol(symbol)
    if not _is_a_share(s):
        raise ProviderError(ProviderErrorCode.INVALID_SYMBOL, f"not an A-share: {s}")
    code, suffix = s.split(".", 1)
    market = "1" if suffix == "SS" else "0"
    return f"{market}.{code}"


def _http_get_json(url: str, timeout: float = 8.0) -> dict[str, Any]:
    """Plain stdlib HTTP GET (no extra deps). Retries on transient failures."""
    headers = {
        "User-Agent": "Mozilla/5.0 (TradingAgents)",
        "Accept": "application/json",
        "Referer": "https://quote.eastmoney.com/",
    }
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_exc = exc
            time.sleep(0.4 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("eastmoney http failed")


# EastMoney field codes we request. See https://push2.eastmoney.com/ for the spec.
# With invt=2&fltt=2 the values come back already divided (e.g. f43=20.11 means
# ¥20.11, not 2011). Field meanings:
# f43: latest price (¥), f44: high (¥), f45: low (¥),
# f46: open (¥), f60: previous close (¥), f169: change amount (¥),
# f170: change percent (percent units — e.g. 1.5 = 1.5%),
# f47: volume (in 手, 1 手 = 100 shares), f48: turnover (¥),
# f50: volume ratio, f57: ticker code, f58: ticker name,
# f60: previous close (¥), f86: timestamp (epoch seconds),
# f107: market (int, 1=SH/0=SZ),
# f116: total market cap (¥), f117: circulating cap (¥),
# f162: turnover rate (%), f167: PE ratio (TTM), f168: turnover,
# f169: change amount (¥), f170: change percent (%),
# f171: amplitude (%), f191: volume (in shares).
_QUOTE_FIELDS = "f43,f44,f45,f46,f47,f48,f50,f60,f169,f170,f57,f58,f86,f107,f116,f117,f162,f167,f168,f171"


class EastMoneyProvider:
    """Free real-time A-share quote provider (East Money push2 endpoint).

    No API key required. Provides intraday prices for Shanghai (.SS) and
    Shenzhen (.SZ) listings. Crypto and non-A-share equities fall through
    to the next provider in the chain.
    """

    name = "eastmoney"

    def supports(self, symbol: str, asset_type: str, capability: str) -> bool:
        return asset_type == "stock" and _is_a_share(symbol) and capability in {
            "quote",
            "identity",
        }

    def get_quote(self, symbol: str, asset_type: str) -> QuoteSnapshot:
        secid = _secid(symbol)
        url = (
            "https://push2.eastmoney.com/api/qt/stock/get?"
            + urllib.parse.urlencode({
                "secid": secid,
                "fields": _QUOTE_FIELDS,
                "invt": "2",
                "fltt": "2",
                "cb": "",
                "_": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            })
        )
        try:
            payload = _http_get_json(url)
        except Exception as exc:
            raise ProviderError(ProviderErrorCode.PROVIDER_ERROR, str(exc)) from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not data or not isinstance(data, dict):
            raise ProviderError(ProviderErrorCode.NO_DATA, "eastmoney returned empty data")

        # With invt=2&fltt=2 EastMoney returns every price-like field already
        # divided (e.g. f43=20.11 means ¥20.11, not 2011). We just parse the
        # float directly; no scale detection is needed.
        def num(*codes: str) -> float | None:
            for code in codes:
                raw = data.get(code)
                if raw is None or raw == "-":
                    continue
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
            return None

        price = num("f43")
        if price is None:
            raise ProviderError(ProviderErrorCode.NO_DATA, "no price field")

        as_of_raw = data.get("f86")
        as_of = None
        if as_of_raw not in (None, "-"):
            try:
                as_of = datetime.fromtimestamp(int(as_of_raw), tz=timezone.utc)
            except (TypeError, ValueError):
                as_of = None

        market = data.get("f107")
        # f107 comes back as int (1=SH, 0=SZ) under invt=2&fltt=2; coerce to
        # string before comparing so the comparison is type-stable.
        exchange = "SHH" if str(market) == "1" else ("SHZ" if str(market) == "0" else None)

        name = data.get("f58") or None

        return QuoteSnapshot(
            symbol=_normalize_symbol(symbol),
            asset_type=asset_type,
            price=price,
            open=num("f46"),
            high=num("f44"),
            low=num("f45"),
            previous_close=num("f60"),
            change=num("f169"),
            change_percent=num("f170"),
            # Quantitative metrics (all already divided under invt=2&fltt=2):
            #   f47 is volume in 手 (1 手 = 100 shares) → multiply for shares;
            #   f48 turnover is already in ¥; f50 量比; f116/f117 cap in ¥;
            #   f162 换手率 in %; f167 市盈率(动); f168 turnover 元 (alt);
            #   f171 振幅 in %.
            volume=(num("f47") * 100 if num("f47") is not None else None),
            turnover=num("f48") or num("f168"),
            volume_ratio=num("f50"),
            turnover_rate=num("f162"),
            market_cap=num("f116"),
            circulating_cap=num("f117"),
            pe_ratio=num("f167"),
            amplitude=num("f171"),
            currency="CNY",
            as_of=as_of,
            fetched_at=datetime.now(timezone.utc),
            freshness=Freshness.FRESH,
            is_delayed=False,
            source=self.name,
            market_status="open" if self._looks_open(as_of) else "closed",
            exchange=exchange,
            raw_summary=name,
        )

    def get_identity(self, symbol: str, asset_type: str) -> AssetIdentity:
        snap = self.get_quote(symbol, asset_type)
        return AssetIdentity(
            symbol=snap.symbol,
            asset_type=asset_type,
            name=snap.raw_summary,
            exchange=snap.exchange,
            currency=snap.currency,
        )

    def get_candles(self, symbol: str, interval: str, start, end) -> list[Candle]:
        raise ProviderError(
            ProviderErrorCode.PROVIDER_ERROR,
            "eastmoney candles not implemented; use yfinance",
        )

    @staticmethod
    def _detect_scale(data: dict[str, Any]) -> int:
        """No-op: with invt=2&fltt=2, EastMoney returns prices already divided.

        Kept for API stability. Returns 1 so any historical callers that still
        divide by it produce correct values.
        """
        return 1

    @staticmethod
    def _looks_open(as_of: datetime | None) -> bool:
        if as_of is None:
            return False
        cn = as_of.astimezone(timezone.utc).utcoffset()  # type: ignore[attr-defined]
        # Treat anything within 9 hours of `now` as the latest tick from today.
        delta = abs((datetime.now(timezone.utc) - as_of).total_seconds())
        return delta < 60 * 60 * 9
