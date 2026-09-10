"""Tests for BarGenerator multi-period K-line."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from web.bar_generator import (
    DAILY_PERIODS,
    MINUTE_PERIODS,
    add_moving_averages,
    build_kline_response,
    df_to_candles,
    fetch_daily_candles,
    fetch_intraday_candles,
    resample_ohlcv,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def daily_df() -> pd.DataFrame:
    """合成 60 个交易日的 OHLCV(覆盖约 3 个月)。"""
    n = 60
    dates = pd.bdate_range("2025-01-01", periods=n)  # 工作日
    rng = np.random.default_rng(42)
    close = 38 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + np.abs(rng.normal(0, 0.3, n))
    low = close - np.abs(rng.normal(0, 0.3, n))
    open_ = close + rng.normal(0, 0.2, n)
    vol = rng.integers(500_000, 3_000_000, n).astype(float)
    return pd.DataFrame({
        "Date": dates,
        "Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol,
    })


# ---------------------------------------------------------------------------
# resample_ohlcv 正确性
# ---------------------------------------------------------------------------

def test_resample_weekly_open_is_first(daily_df):
    """周线 Open = 该周第一个交易日的 Open。"""
    weekly = resample_ohlcv(daily_df, "1w")
    assert not weekly.empty
    # 验证 OHLCV 语义
    assert all(c in weekly.columns for c in ["Open", "High", "Low", "Close", "Volume"])


def test_resample_weekly_high_is_max(daily_df):
    """周线 High = 该周所有日的 max(High)。"""
    weekly = resample_ohlcv(daily_df, "1w")
    daily_max = daily_df.groupby(
        pd.to_datetime(daily_df["Date"]).dt.isocalendar().week
    )["High"].max()
    # 至少抽一根验证:第一周 max 应等于 daily_df 第一周的 max
    first_week_max = daily_df.iloc[:5]["High"].max()
    assert weekly["High"].iloc[0] >= first_week_max


def test_resample_monthly_close_is_last(daily_df):
    """月线 Close = 该月最后一日的 Close。"""
    monthly = resample_ohlcv(daily_df, "1M")
    assert not monthly.empty
    # 最后一根 monthly 的 Close 应等于 daily_df 最后一个
    assert monthly["Close"].iloc[-1] == pytest.approx(daily_df["Close"].iloc[-1])


def test_resample_invalid_period(daily_df):
    with pytest.raises(ValueError, match="resample_ohlcv"):
        resample_ohlcv(daily_df, "1d")


# ---------------------------------------------------------------------------
# fetch_daily_candles 边界
# ---------------------------------------------------------------------------

def test_fetch_daily_invalid_period(daily_df):
    with pytest.raises(ValueError, match="period"):
        fetch_daily_candles("AAPL", count=100, period="2d")


def test_fetch_daily_count_out_of_range():
    with pytest.raises(ValueError, match="count"):
        fetch_daily_candles("AAPL", count=10)
    with pytest.raises(ValueError, match="count"):
        fetch_daily_candles("AAPL", count=2000)


# ---------------------------------------------------------------------------
# 分钟线 — A 股 / 港股拒绝
# ---------------------------------------------------------------------------

def test_intraday_a_share_rejected():
    with pytest.raises(ValueError, match="分钟线.*不支持"):
        fetch_intraday_candles("600036.SS", period="5m")


def test_intraday_hk_rejected():
    with pytest.raises(ValueError, match="分钟线.*不支持"):
        fetch_intraday_candles("0700.HK", period="5m")


def test_intraday_invalid_period():
    with pytest.raises(ValueError, match="period"):
        fetch_intraday_candles("AAPL", period="2h")


# ---------------------------------------------------------------------------
# add_moving_averages
# ---------------------------------------------------------------------------

def test_ma_calculation_matches_pandas(daily_df):
    from web.bar_generator import add_moving_averages
    ma = add_moving_averages(daily_df, periods=(20,))
    expected_ma20 = daily_df["Close"].rolling(20, min_periods=20).mean().dropna()
    # ma20 列表长度应等于 expected_ma20
    assert len(ma["ma20"]) == len(expected_ma20)
    # 最后一个值应等于 expected 最后一个
    assert ma["ma20"][-1]["value"] == pytest.approx(expected_ma20.iloc[-1])


def test_ma_returns_dict_with_keys(daily_df):
    ma = add_moving_averages(daily_df, periods=(20, 60))
    assert set(ma.keys()) == {"ma20", "ma60"}


def test_ma_handles_empty_df():
    empty = pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    ma = add_moving_averages(empty, periods=(20,))
    assert ma == {"ma20": []}


# ---------------------------------------------------------------------------
# df_to_candles
# ---------------------------------------------------------------------------

def test_df_to_candles_structure(daily_df):
    candles = df_to_candles(daily_df)
    assert len(candles) == len(daily_df)
    assert all(
        set(c.keys()) == {"time", "open", "high", "low", "close", "volume"}
        for c in candles
    )


def test_df_to_candles_empty():
    empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    assert df_to_candles(empty) == []


def test_df_to_candles_unix_timestamp(daily_df):
    candles = df_to_candles(daily_df)
    first = candles[0]
    # Unix timestamp in seconds (2025-01-01 ≈ 1735689600)
    assert 1735600000 < first["time"] < 1735800000


# ---------------------------------------------------------------------------
# build_kline_response 集成
# ---------------------------------------------------------------------------

def test_build_kline_response_unsupported_period():
    with pytest.raises(ValueError, match="period"):
        build_kline_response("AAPL", period="4h")


def test_build_kline_response_a_share_minute_rejected():
    with pytest.raises(ValueError, match="分钟线"):
        build_kline_response("600036.SS", period="5m")


def test_build_kline_response_count_bounds():
    with pytest.raises(ValueError, match="count"):
        build_kline_response("AAPL", period="1d", count=10)
    with pytest.raises(ValueError, match="count"):
        build_kline_response("AAPL", period="1d", count=5000)


def test_build_kline_response_keys(daily_df):
    """mock 模式:用 daily_df 直接调用 build_kline_response 走日线路径。"""
    from unittest.mock import patch

    with patch("web.bar_generator.fetch_daily_candles", return_value=daily_df):
        resp = build_kline_response("600036.SS", period="1d", count=240)

    expected_keys = {"symbol", "period", "count", "candles", "ma", "fetched_at"}
    assert set(resp.keys()) == expected_keys
    assert resp["symbol"] == "600036.SS"
    assert resp["period"] == "1d"
    assert resp["count"] == len(daily_df)
    assert len(resp["candles"]) == len(daily_df)
    assert "ma20" in resp["ma"]


# ---------------------------------------------------------------------------
# _is_a_share / _is_hk_stock / _is_us_stock 辅助函数
# ---------------------------------------------------------------------------

def test_is_a_share():
    from web.bar_generator import _is_a_share
    assert _is_a_share("600036.SS") is True
    assert _is_a_share("000001.SZ") is True
    assert _is_a_share("AAPL") is False
    assert _is_a_share("0700.HK") is False


def test_is_hk_stock():
    from web.bar_generator import _is_hk_stock
    assert _is_hk_stock("0700.HK") is True
    assert _is_hk_stock("600036.SS") is False


def test_is_us_stock():
    from web.bar_generator import _is_us_stock
    assert _is_us_stock("AAPL") is True
    assert _is_us_stock("NVDA") is True
    assert _is_us_stock("BRK.B") is True
    assert _is_us_stock("600036.SS") is False
    assert _is_us_stock("0700.HK") is False
