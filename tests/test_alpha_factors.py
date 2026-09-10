"""Tests for alpha158 因子计算 + IC/IR 评估。

覆盖:
1. 因子计算正确性(用合成 OHLCV,手算预期)
2. NaN warmup 处理
3. IC 评估:合成完美预测序列 → IC 应接近 1
4. IC 评估:随机序列 → IC 应接近 0
5. 错误路径(未知因子、缺列、sample 不足)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradingagents.dataflows.alpha_factors import (
    CATEGORIES,
    REGISTRY,
    compute_factor,
    compute_factors,
    evaluate_factor,
    get_spec,
    list_factors,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def uptrend_df() -> pd.DataFrame:
    """合成 100 日单调上涨序列(Close 从 100 → 200,等差)。"""
    n = 100
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    close = np.linspace(100.0, 200.0, n)
    df = pd.DataFrame({
        "Date": dates,
        "Open": close - 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": np.full(n, 1_000_000.0),
    })
    return df


@pytest.fixture
def downtrend_df() -> pd.DataFrame:
    """合成 100 日单调下跌序列。"""
    n = 100
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    close = np.linspace(200.0, 100.0, n)
    df = pd.DataFrame({
        "Date": dates,
        "Open": close + 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": np.full(n, 1_000_000.0),
    })
    return df


@pytest.fixture
def random_df(seed: int = 42) -> pd.DataFrame:
    """合成 500 日随机序列(用固定种子,便于复现)。"""
    n = 500
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + np.abs(rng.normal(0, 0.5, n))
    low = close - np.abs(rng.normal(0, 0.5, n))
    open_ = close + rng.normal(0, 0.3, n)
    vol = rng.integers(500_000, 2_000_000, n).astype(float)
    return pd.DataFrame({
        "Date": dates,
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": vol,
    })


# ---------------------------------------------------------------------------
# 1. Registry 完整性
# ---------------------------------------------------------------------------

def test_registry_has_32_factors():
    assert len(REGISTRY) == 32


def test_registry_covers_4_categories():
    cats = {s.category for s in REGISTRY.values()}
    assert cats == {"momentum", "volatility", "volume_price", "trend"}


def test_list_factors_all():
    factors = list_factors()
    assert len(factors) == 32


def test_list_factors_filter_category():
    momentum = list_factors("momentum")
    assert all(s.category == "momentum" for s in momentum)
    assert len(momentum) >= 10

    with pytest.raises(ValueError, match="未知类别"):
        list_factors("nonexistent")


def test_get_spec_unknown_raises():
    with pytest.raises(ValueError, match="未知因子"):
        get_spec("not_a_real_factor")


# ---------------------------------------------------------------------------
# 2. 动量因子 — 正确性
# ---------------------------------------------------------------------------

def test_roc_5_on_uptrend(uptrend_df):
    """单调上涨:5 日 ROC 后期应 > 0 且接近日均涨幅。"""
    s = compute_factor(uptrend_df, "roc_5")
    # Uptrend: 100 → 200 over 100 days, daily ≈ 1.02%, 5-day ≈ 5.2%
    tail = s.dropna()
    assert (tail > 0).all(), "上涨序列 ROC_5 应 > 0"
    # 后期 5 日变化率应接近 (200-100)/100 * 5 ≈ 5.05(从 100 到 200 / 100 天 / 5)
    # 实际:从 day 50 (close=150) 到 day 55 (close=155) = 5/150 ≈ 3.3%
    # 较严格检查中间段
    mid = s.iloc[50]
    assert 0.03 < mid < 0.04, f"中期 ROC_5 应在 3-4%, 实际 {mid:.4f}"


def test_roc_5_on_downtrend(downtrend_df):
    s = compute_factor(downtrend_df, "roc_5")
    tail = s.dropna()
    assert (tail < 0).all(), "下跌序列 ROC_5 应 < 0"


def test_momentum_5(uptrend_df):
    """mom_5 = close - close.shift(5); 上涨序列后期应为正且递增。"""
    s = compute_factor(uptrend_df, "momentum_5")
    tail = s.dropna()
    assert tail.iloc[-1] > 0
    # Day 95 vs day 90: close[95]≈197.98, close[90]≈191.92, mom ≈ 6.06
    assert 4.5 < tail.iloc[-1] < 5.5, f"momentum_5 后期应 u2248 5.05, 实际 {tail.iloc[-1]:.3f}"


def test_rsi_24_on_strong_uptrend(uptrend_df):
    """持续上涨 → RSI 应 > 70(超买)。"""
    s = compute_factor(uptrend_df, "rsi_24")
    tail = s.dropna().iloc[-20:]
    assert tail.mean() > 70, f"持续上涨 RSI(24) 应 > 70, 实际 {tail.mean():.2f}"


def test_rsi_24_on_strong_downtrend(downtrend_df):
    """持续下跌 → RSI 应 < 30(超卖)。"""
    s = compute_factor(downtrend_df, "rsi_24")
    tail = s.dropna().iloc[-20:]
    assert tail.mean() < 30, f"持续下跌 RSI(24) 应 < 30, 实际 {tail.mean():.2f}"


def test_macd_definition(uptrend_df):
    """MACD = EMA12 - EMA26,验证手算值。"""
    close = uptrend_df["Close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    expected = ema12 - ema26
    actual = compute_factor(uptrend_df, "macd")
    np.testing.assert_allclose(actual.values, expected.values, rtol=1e-10)


def test_macd_hist_is_macd_minus_signal(uptrend_df):
    """数学定义:MACD_hist = MACD - MACD_signal。"""
    macd = compute_factor(uptrend_df, "macd")
    sig = compute_factor(uptrend_df, "macd_signal")
    hist = compute_factor(uptrend_df, "macd_hist")
    np.testing.assert_allclose(hist.values, (macd - sig).values, rtol=1e-10)


# ---------------------------------------------------------------------------
# 3. 波动率因子 — 正确性
# ---------------------------------------------------------------------------

def test_atr_14_positive(uptrend_df):
    """ATR 始终 ≥ 0。"""
    s = compute_factor(uptrend_df, "atr_14").dropna()
    assert (s >= 0).all()


def test_std_5_matches_pandas(uptrend_df):
    close = uptrend_df["Close"]
    expected = close.rolling(5, min_periods=5).std()
    actual = compute_factor(uptrend_df, "std_5")
    np.testing.assert_allclose(actual.values, expected.values, rtol=1e-10)


def test_boll_pct_b_at_zero_for_constant_close(random_df):
    """完全平直的序列(Close 全等于常数), %b 应 ≈ 0(std=0 → NaN,我们的实现返回 NaN)。"""
    df = random_df.copy()
    df["Close"] = 100.0  # 完全平直
    df["High"] = 100.0
    df["Low"] = 100.0
    df["Open"] = 100.0
    s = compute_factor(df, "boll_pct_b")
    # std=0 → NaN(预期:无信号)
    assert s.dropna().empty or s.dropna().iloc[-1] == 0, "平直序列 %b 应为 NaN 或 0"


def test_boll_pct_b_definition_correctness():
    """验证 %b 公式与手算一致。"""
    n = 30
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame({
        "Date": dates,
        "Open": close, "High": close + 1, "Low": close - 1,
        "Close": close, "Volume": np.full(n, 1_000_000.0),
    })
    s = compute_factor(df, "boll_pct_b").dropna()
    # 手算最后一个 %b
    ma = df["Close"].rolling(20).mean().iloc[-1]
    std = df["Close"].rolling(20).std().iloc[-1]
    last_close = df["Close"].iloc[-1]
    expected = (last_close - ma) / (2.0 * std) if std > 0 else 0
    actual = s.iloc[-1]
    np.testing.assert_allclose(actual, expected, rtol=1e-9)


# ---------------------------------------------------------------------------
# 4. 量价因子 — 正确性
# ---------------------------------------------------------------------------

def test_obv_uptrend_increasing(uptrend_df):
    """上涨序列 OBV 应单调递增(无跳空情况下)。"""
    s = compute_factor(uptrend_df, "obv")
    diffs = s.diff().dropna()
    assert (diffs > 0).all(), "上涨序列 OBV 增量应 > 0"


def test_vwap_5_close_to_close_simple(random_df):
    """VWAP 5 应在窗口期 [min(Low), max(High)] 之间(数学上正确的不等式)。"""
    df = random_df.reset_index(drop=True)
    s = compute_factor(df, "vwap_5").reset_index(drop=True).dropna()
    high = df["High"].reset_index(drop=True)
    low = df["Low"].reset_index(drop=True)
    n = min(100, len(s))
    s_tail = s.iloc[-n:].values
    # 滚动窗口的 min Low 和 max High
    rolling_min_low = low.rolling(5, min_periods=1).min().iloc[-n:].values
    rolling_max_high = high.rolling(5, min_periods=1).max().iloc[-n:].values
    valid = ~np.isnan(s_tail)
    assert (s_tail[valid] >= rolling_min_low[valid]).all(), "VWAP < 窗口期 min(Low)"
    assert (s_tail[valid] <= rolling_max_high[valid]).all(), "VWAP > 窗口期 max(High)"


def test_volume_ratio_5_equals_one_for_constant_volume(uptrend_df):
    """uptrend_df Volume 恒定 → 量比应 ≈ 1。"""
    s = compute_factor(uptrend_df, "volume_ratio_5").dropna()
    np.testing.assert_allclose(s.values, 1.0, rtol=1e-10)


# ---------------------------------------------------------------------------
# 5. 趋势因子 — 正确性
# ---------------------------------------------------------------------------

def test_adx_14_in_range(uptrend_df):
    """ADX 应在 [0, 100] 区间。"""
    s = compute_factor(uptrend_df, "adx_14").dropna()
    assert (s >= 0).all() and (s <= 100).all(), f"ADX 越界: min={s.min():.2f}, max={s.max():.2f}"


def test_aroon_up_at_100_when_just_made_high(random_df):
    """当今天 = N 日内最高点时,Aroon Up = 100。"""
    df = random_df.copy().reset_index(drop=True)
    # 取最后 25 日,设最后一天为最高
    df.loc[df.index[-1], "High"] = df["High"].iloc[-25:].max() + 100
    s = compute_factor(df, "aroon_up_25").dropna()
    assert s.iloc[-1] == 100.0


# ---------------------------------------------------------------------------
# 6. NaN warmup 处理
# ---------------------------------------------------------------------------

def test_roc_5_first_5_rows_are_nan(uptrend_df):
    """ROC(5) 前 5 行应 NaN(没有 5 日历史)。"""
    s = compute_factor(uptrend_df, "roc_5")
    assert s.iloc[:5].isna().all()


def test_macd_stable_after_warmup(uptrend_df):
    """EMA 在 adjust=False 下第一行就有值;验证后期值稳定(无 NaN)。"""
    s = compute_factor(uptrend_df, "macd")
    tail = s.iloc[26:].dropna()
    assert len(tail) > 50, f"MACD 后段应有值,实际只有 {len(tail)} 行"
    # EMA 在 adjust=False 下从第一行就计算,所以不应有 NaN
    assert s.notna().sum() > 50


# ---------------------------------------------------------------------------
# 7. 批量计算
# ---------------------------------------------------------------------------

def test_compute_factors_returns_dataframe(uptrend_df):
    out = compute_factors(uptrend_df, ["roc_5", "macd"])  # 避免 rsi Wilder 处理
    assert isinstance(out, pd.DataFrame)
    assert set(out.columns) == {"roc_5", "macd"}
    assert len(out) == len(uptrend_df)


def test_compute_factors_dedup_and_preserve_order(uptrend_df):
    out = compute_factors(uptrend_df, ["roc_5", "roc_5", "macd"])
    assert list(out.columns) == ["roc_5", "macd"]


# ---------------------------------------------------------------------------
# 8. IC 评估 — 合成数据
# ---------------------------------------------------------------------------

def test_evaluate_factor_perfect_predictor():
    """合成:factor_t = forward_ret_t(无噪声)→ IC 应 ≈ 1。"""
    n = 500
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame({
        "Date": dates,
        "Open": close, "High": close + 1, "Low": close - 1,
        "Close": close, "Volume": np.full(n, 1_000_000.0),
    })
    # 注入 factor = forward_ret 作为"完美预测"
    forward_ret = np.log(df["Close"].shift(-5) / df["Close"]).rename("perfect_factor")

    from tradingagents.dataflows.alpha_factors import evaluate_factor as ef
    res = ef(df, forward_ret, forward_days=5)
    assert res["n_samples"] >= 400
    # 完美预测 IC 应该非常接近 1
    assert res["ic"] > 0.95, f"完美预测 IC 应 > 0.95, 实际 {res['ic']:.4f}"
    assert res["rank_ic"] > 0.95


def test_evaluate_factor_random_returns_near_zero_ic():
    """完全随机的 factor,IC 应接近 0。"""
    n = 500
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame({
        "Date": dates,
        "Open": close, "High": close + 1, "Low": close - 1,
        "Close": close, "Volume": np.full(n, 1_000_000.0),
    })
    rand_factor = pd.Series(rng.normal(0, 1, n), name="random_factor", index=df.index)
    from tradingagents.dataflows.alpha_factors import evaluate_factor as ef
    res = ef(df, rand_factor, forward_days=5)
    # 随机因子 IC 的绝对值应该 < 0.1(统计上)
    assert abs(res["ic"]) < 0.10, f"随机因子 IC 应接近 0, 实际 {res['ic']:.4f}"


def test_evaluate_factor_returns_dict_keys(random_df):
    res = evaluate_factor(random_df, "roc_5", forward_days=5)
    expected = {"ic", "rank_ic", "ic_std", "ic_positive_ratio", "ir", "n_samples"}
    assert set(res.keys()) == expected


def test_evaluate_factor_insufficient_samples():
    """样本不足(< 30)应返回 NaN,而不是崩溃。"""
    n = 20
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    df = pd.DataFrame({
        "Date": dates,
        "Open": np.linspace(100, 110, n),
        "High": np.linspace(101, 111, n),
        "Low": np.linspace(99, 109, n),
        "Close": np.linspace(100, 110, n),
        "Volume": np.full(n, 1_000_000.0),
    })
    res = evaluate_factor(df, "roc_5", forward_days=5)
    assert res["n_samples"] < 30
    assert np.isnan(res["ic"])


def test_evaluate_factor_invalid_forward_days(random_df):
    with pytest.raises(ValueError, match="forward_days"):
        evaluate_factor(random_df, "roc_5", forward_days=0)


# ---------------------------------------------------------------------------
# 9. 错误处理
# ---------------------------------------------------------------------------

def test_compute_factor_missing_close_column():
    """缺少 Close 列应抛错。"""
    df = pd.DataFrame({"Open": [1, 2, 3], "Volume": [1, 2, 3]})
    with pytest.raises(ValueError, match="Close"):
        compute_factor(df, "roc_5")


def test_compute_factor_missing_ohlcv_for_ohlc_based():
    """ATR 需要完整 OHLCV(用 High/Low),缺列应抛错。"""
    df = pd.DataFrame({"Date": pd.date_range("2025-01-01", periods=30), "Close": range(30)})
    with pytest.raises(ValueError, match="OHLCV"):
        compute_factor(df, "atr_14")


def test_compute_factor_unknown_raises():
    df = pd.DataFrame({"Close": [1.0]})
    with pytest.raises(ValueError, match="未知因子"):
        compute_factor(df, "fake_factor")
