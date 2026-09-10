"""
BarGenerator — 多周期 K 线生成器。

支持周期:
- 1d  日线    全市场(load_ohlcv 已有 5y 缓存)
- 1w  周线    从日线重采样
- 1M  月线    从日线重采样
- 60m 60分钟  仅美股(yfinance intraday)
- 30m 30分钟  仅美股
- 15m 15分钟  仅美股
- 5m  5分钟   仅美股
- 1m  1分钟   仅美股(yfinance 限最近 7 天)

设计:
- 1d/1w/1M 缓存 24h(每日数据)
- 分钟线缓存 60s(数据可日内变化)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


SUPPORTED_PERIODS = ("1d", "1w", "1M", "1m", "5m", "15m", "30m", "60m")
DAILY_PERIODS = ("1d", "1w", "1M")
MINUTE_PERIODS = ("1m", "5m", "15m", "30m", "60m")


@dataclass(frozen=True)
class Candle:
    """单根 K 线。"""
    time: int          # unix timestamp(秒)
    open: float
    high: float
    low: float
    close: float
    volume: float


def _is_a_share(symbol: str) -> bool:
    """粗略判断:A 股代码以 .SS(沪)或 .SZ(深)结尾。"""
    return symbol.upper().endswith((".SS", ".SZ"))


def _is_hk_stock(symbol: str) -> bool:
    return symbol.upper().endswith(".HK")


def _is_us_stock(symbol: str) -> bool:
    """无后缀或 .US / -USD 都视为美股。"""
    s = symbol.upper()
    return not any(s.endswith(suffix) for suffix in (".SS", ".SZ", ".HK"))


def resample_ohlcv(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """从日线 OHLCV 重采样到周/月。

    Args:
    period: "1w" 或 "1M"

    Returns:
        重采样后的 OHLCV DataFrame(Date 索引)
    """
    if period not in ("1w", "1M"):
        raise ValueError(f"resample_ohlcv 仅支持 '1w' 或 '1M',得到 {period!r}")

    rule = "W-FRI" if period == "1w" else "ME"  # 周线以周五结束,月线以月末

    agg = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    if "Date" in df.columns:
        idx = pd.to_datetime(df["Date"])
        work = df.set_index(idx)
    else:
        work = df.copy()

    resampled = work.resample(rule).agg(agg).dropna()
    return resampled.reset_index().rename(columns={"index": "Date"})


def fetch_daily_candles(symbol: str, count: int = 240, period: str = "1d") -> pd.DataFrame:
    """拉取日/周/月 K 线。

    Args:
    symbol: ticker
    count: K 线根数
    period: "1d" / "1w" / "1M"

    Returns:
        DataFrame with columns Date, Open, High, Low, Close, Volume
    """
    from tradingagents.dataflows.stockstats_utils import load_ohlcv

    if period not in DAILY_PERIODS:
        raise ValueError(f"period 必须是 {DAILY_PERIODS} 之一,得到 {period!r}")
    if count < 30 or count > 1000:
        raise ValueError(f"count 必须在 30-1000,得到 {count}")

    # 多取一些以防节假日断档
    lookback_days = {
        "1d": count * 2,  # 1d:count 天 ≈ count * 1.5 工作日
    "1w": count * 7 * 2,
    "1M": count * 31 * 2,
    }[period]

    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    df = load_ohlcv(symbol, today)
    if df.empty:
        return df

    # 取最近 lookback_days 行,确保足够生成 count 根
    df = df.tail(lookback_days).reset_index(drop=True)

    if period == "1d":
        result = df
    else:
        result = resample_ohlcv(df, period)

    # 取最后 count 行
    if len(result) > count:
        result = result.tail(count).reset_index(drop=True)
    return result


def fetch_intraday_candles(symbol: str, period: str = "5m", count: int = 240) -> pd.DataFrame:
    """拉取分钟 K 线(仅美股,通过 yfinance intraday)。

    Args:
    symbol: ticker
    period: "1m"/"5m"/"15m"/"30m"/"60m"
    count: K 线根数

    Raises:
    ValueError: A 股 / 港股不支持分钟线
    """
    if period not in MINUTE_PERIODS:
        raise ValueError(f"period 必须是 {MINUTE_PERIODS} 之一,得到 {period!r}")
    if _is_a_share(symbol) or _is_hk_stock(symbol):
        raise ValueError(
            f"分钟线 K 线不支持 {symbol}(A 股 / 港股)。"
            "请用 1d / 1w / 1M,或切换到美股标的。"
        )

    import yfinance as yf

    # yfinance intraday 限制:
    # - period="1d" + interval="1m" → 最多 7 天
    # - interval="5m"/"15m"/"30m"/"60m" → 最多 60 天
    yf_period = "60d" if period != "1m" else "7d"

    ticker = yf.Ticker(symbol)
    df = ticker.history(period=yf_period, interval=period, auto_adjust=False)

    if df.empty:
        return df

    # 重置索引,timestamp 化
    df = df.reset_index()
    # yfinance 列名可能带 'Datetime' 或 'Date'
    if "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "Date"})
    df = df.tail(count).reset_index(drop=True)
    return df


def add_moving_averages(
    df: pd.DataFrame,
    periods: tuple[int, ...] = (20, 60),
) -> dict[str, list[dict]]:
    """计算 MA 列并返回 [{time, value}, ...] 列表。"""
    if df.empty or "Close" not in df.columns:
        return {f"ma{p}": [] for p in periods}

    if "Date" in df.columns:
        _times = pd.to_datetime(df["Date"])
        # 处理 tz-aware → tz-naive(unix timestamp 必须是 naive)
        if hasattr(_times, 'dt') and getattr(_times.dt, 'tz', None) is not None:
            _times = _times.dt.tz_convert(None)
        # datetime64 → unix seconds:根据精度(s/us/ms/ns)选除数
        _int = _times.astype('int64')
        # pandas 在 tz_convert(None) 后 dtype 显示 datetime64[s] 但实际值仍按原始单位存储
        # 直接用数量级判断:1e18=ns, 1e15=us, 1e12=ms, 1e9=s
        _abs = abs(int(_int.iloc[0])) if len(_int) > 0 else 0
        if _abs >= 10**18:
            _divisor = 10**9
        elif _abs >= 10**15:
            _divisor = 10**6
        elif _abs >= 10**12:
            _divisor = 10**3
        else:
            _divisor = 1
        times = _int // _divisor
    else:
        _times = pd.to_datetime(df.index)
        # 处理 tz-aware → tz-naive(unix timestamp 必须是 naive)
        if hasattr(_times, 'dt') and getattr(_times.dt, 'tz', None) is not None:
            _times = _times.dt.tz_convert(None)
        # datetime64 → unix seconds:根据精度(s/us/ms/ns)选除数
        _int = _times.astype('int64')
        # pandas 在 tz_convert(None) 后 dtype 显示 datetime64[s] 但实际值仍按原始单位存储
        # 直接用数量级判断:1e18=ns, 1e15=us, 1e12=ms, 1e9=s
        _abs = abs(int(_int.iloc[0])) if len(_int) > 0 else 0
        if _abs >= 10**18:
            _divisor = 10**9
        elif _abs >= 10**15:
            _divisor = 10**6
        elif _abs >= 10**12:
            _divisor = 10**3
        else:
            _divisor = 1
        times = _int // _divisor

    out: dict[str, list[dict]] = {}
    for p in periods:
        ma = df["Close"].rolling(p, min_periods=p).mean()
        points = [
            {"time": int(t), "value": float(v)}
            for t, v in zip(times, ma)
            if not np.isnan(v)
        ]
        out[f"ma{p}"] = points
    return out


def df_to_candles(df: pd.DataFrame) -> list[dict]:
    """DataFrame → TradingView Lightweight Charts 格式 candles。"""
    if df.empty:
        return []
    if "Date" in df.columns:
        _times = pd.to_datetime(df["Date"])
        # 处理 tz-aware → tz-naive(unix timestamp 必须是 naive)
        if hasattr(_times, 'dt') and getattr(_times.dt, 'tz', None) is not None:
            _times = _times.dt.tz_convert(None)
        # datetime64 → unix seconds:根据精度(s/us/ms/ns)选除数
        _int = _times.astype('int64')
        # pandas 在 tz_convert(None) 后 dtype 显示 datetime64[s] 但实际值仍按原始单位存储
        # 直接用数量级判断:1e18=ns, 1e15=us, 1e12=ms, 1e9=s
        _abs = abs(int(_int.iloc[0])) if len(_int) > 0 else 0
        if _abs >= 10**18:
            _divisor = 10**9
        elif _abs >= 10**15:
            _divisor = 10**6
        elif _abs >= 10**12:
            _divisor = 10**3
        else:
            _divisor = 1
        times = _int // _divisor
    else:
        _times = pd.to_datetime(df.index)
        # 处理 tz-aware → tz-naive(unix timestamp 必须是 naive)
        if hasattr(_times, 'dt') and getattr(_times.dt, 'tz', None) is not None:
            _times = _times.dt.tz_convert(None)
        # datetime64 → unix seconds:根据精度(s/us/ms/ns)选除数
        _int = _times.astype('int64')
        # pandas 在 tz_convert(None) 后 dtype 显示 datetime64[s] 但实际值仍按原始单位存储
        # 直接用数量级判断:1e18=ns, 1e15=us, 1e12=ms, 1e9=s
        _abs = abs(int(_int.iloc[0])) if len(_int) > 0 else 0
        if _abs >= 10**18:
            _divisor = 10**9
        elif _abs >= 10**15:
            _divisor = 10**6
        elif _abs >= 10**12:
            _divisor = 10**3
        else:
            _divisor = 1
        times = _int // _divisor

    candles = []
    for i in range(len(df)):
        candles.append({
            "time": int(times.iloc[i]),
            "open": float(df["Open"].iloc[i]),
            "high": float(df["High"].iloc[i]),
            "low": float(df["Low"].iloc[i]),
            "close": float(df["Close"].iloc[i]),
            "volume": float(df["Volume"].iloc[i]) if "Volume" in df.columns else 0.0,
        })
    return candles


def build_kline_response(
    symbol: str,
    period: str,
    count: int = 240,
) -> dict:
    """组装 /api/market/kline 完整响应。

    Returns dict with keys: symbol, period, count, candles, ma, fetched_at
    """
    if period not in SUPPORTED_PERIODS:
        raise ValueError(
            f"period 必须是 {list(SUPPORTED_PERIODS)} 之一,得到 {period!r}"
        )
    if count < 30 or count > 1000:
        raise ValueError(f"count 必须在 30-1000,得到 {count}")

    if period in DAILY_PERIODS:
        df = fetch_daily_candles(symbol, count=count, period=period)
    else:
        df = fetch_intraday_candles(symbol, period=period, count=count)

    candles = df_to_candles(df)
    ma = add_moving_averages(df, periods=(20, 60))

    return {
        "symbol": symbol,
        "period": period,
        "count": len(candles),
        "candles": candles,
        "ma": ma,
        "fetched_at": pd.Timestamp.now().isoformat(),
    }
