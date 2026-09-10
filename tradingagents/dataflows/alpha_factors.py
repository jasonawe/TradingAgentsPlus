"""
Alpha158 因子计算库 — 从 vnpy.alpha / qlib alpha158 借鉴而来。

仅搬运因子计算公式 + IC/IR 评估,不搬运 ML 训练框架。

用法:
    df = load_ohlcv(symbol, curr_date)  # 拿 5 年 OHLCV
    factors = compute_factors(df, ["roc_5", "rsi_14", "macd"])
    eval_result = evaluate_factor(df, "roc_5", forward_days=5)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 因子规格 — 一个 category 一组 factor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FactorSpec:
    name: str
    category: str
    description: str
    func: Callable[[pd.DataFrame], pd.Series] = field(compare=False, repr=False)


def _require_close(df: pd.DataFrame) -> pd.Series:
    if "Close" not in df.columns:
        raise ValueError("OHLCV DataFrame 必须有 'Close' 列")
    return df["Close"]


def _require_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    needed = {"Open", "High", "Low", "Close", "Volume"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"OHLCV DataFrame 缺列: {missing}")
    return df


# ---------------------------------------------------------------------------
# 动量类 — Momentum
# ---------------------------------------------------------------------------

def _roc(n: int) -> Callable[[pd.DataFrame], pd.Series]:
    def _impl(df: pd.DataFrame) -> pd.Series:
        close = _require_close(df)
        return close.pct_change(periods=n, fill_method=None)
    return _impl


def _momentum(n: int) -> Callable[[pd.DataFrame], pd.Series]:
    def _impl(df: pd.DataFrame) -> pd.Series:
        close = _require_close(df)
        return close - close.shift(n)
    return _impl


def _rsi(n: int) -> Callable[[pd.DataFrame], pd.Series]:
    """RSI(Wilder 平滑):处理 loss=0 边界返回 100(完美上涨)。"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        close = _require_close(df)
        delta = close.diff()
        gain = delta.clip(lower=0.0).to_numpy()
        loss = (-delta).clip(lower=0.0).to_numpy()

        avg_gain = np.full(len(close), np.nan)
        avg_loss = np.full(len(close), np.nan)

        if len(close) < n + 1:
            return pd.Series(avg_gain, index=df.index)

        # Wilder 平滑:SMA 初始化前 n 个,然后递归
        avg_gain[n] = np.mean(gain[1:n + 1])
        avg_loss[n] = np.mean(loss[1:n + 1])
        for i in range(n + 1, len(close)):
            avg_gain[i] = (avg_gain[i - 1] * (n - 1) + gain[i]) / n
            avg_loss[i] = (avg_loss[i - 1] * (n - 1) + loss[i]) / n

        with np.errstate(divide="ignore", invalid="ignore"):
            rs = np.where(avg_loss == 0, np.inf, avg_gain / np.where(avg_loss == 0, 1, avg_loss))
        rsi = 100.0 - 100.0 / (1.0 + rs)
        # loss 全 0(完美上涨) → RSI = 100;gain 全 0(完美下跌) → RSI = 0
        rsi = np.where(avg_loss == 0, 100.0, rsi)
        rsi = np.where(avg_gain == 0, 0.0, rsi)
        return pd.Series(rsi, index=df.index)
    return _impl


def _macd() -> Callable[[pd.DataFrame], pd.Series]:
    """MACD line (12 - 26 EMA)"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        close = _require_close(df)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        return ema12 - ema26
    return _impl


def _macd_signal() -> Callable[[pd.DataFrame], pd.Series]:
    def _impl(df: pd.DataFrame) -> pd.Series:
        close = _require_close(df)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        return macd.ewm(span=9, adjust=False).mean()
    return _impl


def _macd_hist() -> Callable[[pd.DataFrame], pd.Series]:
    def _impl(df: pd.DataFrame) -> pd.Series:
        close = _require_close(df)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        return macd - signal
    return _impl


# ---------------------------------------------------------------------------
# 波动率类 — Volatility
# ---------------------------------------------------------------------------

def _atr(n: int) -> Callable[[pd.DataFrame], pd.Series]:
    """Average True Range"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        ohlcv = _require_ohlcv(df)
        high = ohlcv["High"]
        low = ohlcv["Low"]
        close = ohlcv["Close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    return _impl


def _std(n: int) -> Callable[[pd.DataFrame], pd.Series]:
    def _impl(df: pd.DataFrame) -> pd.Series:
        close = _require_close(df)
        return close.rolling(n, min_periods=n).std()
    return _impl


def _histvol(n: int) -> Callable[[pd.DataFrame], pd.Series]:
    """年化历史波动率(基于对数收益率,乘以 sqrt(252))"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        close = _require_close(df)
        log_ret = np.log(close / close.shift(1))
        return log_ret.rolling(n, min_periods=n).std() * math.sqrt(252.0)
    return _impl


def _boll(n: int = 20, k: float = 2.0) -> Callable[[pd.DataFrame], pd.Series]:
    def _impl(df: pd.DataFrame) -> pd.Series:
        close = _require_close(df)
        ma = close.rolling(n, min_periods=n).mean()
        std = close.rolling(n, min_periods=n).std()
        return (close - ma) / (k * std).replace(0, np.nan)
    return _impl


# ---------------------------------------------------------------------------
# 量价类 — Volume-Price
# ---------------------------------------------------------------------------

def _obv() -> Callable[[pd.DataFrame], pd.Series]:
    """On-Balance Volume"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        ohlcv = _require_ohlcv(df)
        close = ohlcv["Close"]
        vol = ohlcv["Volume"]
        direction = np.sign(close.diff()).fillna(0.0)
        return (direction * vol).cumsum()
    return _impl


def _vpt() -> Callable[[pd.DataFrame], pd.Series]:
    """Volume-Price Trend"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        ohlcv = _require_ohlcv(df)
        close = ohlcv["Close"]
        vol = ohlcv["Volume"]
        pct_change = close.pct_change(fill_method=None).fillna(0.0)
        return (pct_change * vol).cumsum()
    return _impl


def _vwap(n: int) -> Callable[[pd.DataFrame], pd.Series]:
    """Rolling Volume-Weighted Average Price (n 天滚动)"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        ohlcv = _require_ohlcv(df)
        typical = (ohlcv["High"] + ohlcv["Low"] + ohlcv["Close"]) / 3.0
        pv = typical * ohlcv["Volume"]
        rolling_pv = pv.rolling(n, min_periods=1).sum()
        rolling_vol = ohlcv["Volume"].rolling(n, min_periods=1).sum()
        return rolling_pv / rolling_vol.replace(0, np.nan)
    return _impl


def _volume_ratio(n: int = 5) -> Callable[[pd.DataFrame], pd.Series]:
    """量比 = 当日成交量 / N 日均量"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        ohlcv = _require_ohlcv(df)
        vol = ohlcv["Volume"]
        avg = vol.rolling(n, min_periods=n).mean()
        return vol / avg.replace(0, np.nan)
    return _impl


def _pvt() -> Callable[[pd.DataFrame], pd.Series]:
    """Price-Volume Trend(归一化版)"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        ohlcv = _require_ohlcv(df)
        close = ohlcv["Close"]
        vol = ohlcv["Volume"]
        prev_close = close.shift(1)
        pct = (close - prev_close) / prev_close.replace(0, np.nan)
        return (pct.fillna(0.0) * vol).cumsum()
    return _impl


# ---------------------------------------------------------------------------
# 趋势类 — Trend
# ---------------------------------------------------------------------------

def _adx(n: int = 14) -> Callable[[pd.DataFrame], pd.Series]:
    """Average Directional Index (Wilder smoothing)"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        ohlcv = _require_ohlcv(df)
        high = ohlcv["High"]
        low = ohlcv["Low"]
        close = ohlcv["Close"]

        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        atr = tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
        plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / n, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / n, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    return _impl


def _cci(n: int = 20) -> Callable[[pd.DataFrame], pd.Series]:
    """Commodity Channel Index"""
    def _impl(df: pd.DataFrame) -> pd.Series:
        ohlcv = _require_ohlcv(df)
        tp = (ohlcv["High"] + ohlcv["Low"] + ohlcv["Close"]) / 3.0
        ma = tp.rolling(n, min_periods=n).mean()
        md = tp.rolling(n, min_periods=n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
        return (tp - ma) / (0.015 * md.replace(0, np.nan))
    return _impl


def _aroon_up(n: int = 25) -> Callable[[pd.DataFrame], pd.Series]:
    def _impl(df: pd.DataFrame) -> pd.Series:
        ohlcv = _require_ohlcv(df)
        high = ohlcv["High"]
        # 距离 n 日内最高点的天数
        days_since_high = high.rolling(n, min_periods=n).apply(lambda x: n - 1 - np.argmax(x), raw=True)
        return 100.0 * (n - days_since_high) / n
    return _impl


def _aroon_down(n: int = 25) -> Callable[[pd.DataFrame], pd.Series]:
    def _impl(df: pd.DataFrame) -> pd.Series:
        ohlcv = _require_ohlcv(df)
        low = ohlcv["Low"]
        days_since_low = low.rolling(n, min_periods=n).apply(lambda x: n - 1 - np.argmin(x), raw=True)
        return 100.0 * (n - days_since_low) / n
    return _impl


# ---------------------------------------------------------------------------
# 注册所有因子
# ---------------------------------------------------------------------------

def _build_registry() -> Dict[str, FactorSpec]:
    specs: List[FactorSpec] = []

    # 动量 — 9 个
    for n in (1, 5, 10, 20, 60):
        specs.append(FactorSpec(f"roc_{n}", "momentum", f"{n}日变化率(收益率)", _roc(n)))
    for n in (5, 10, 20):
        specs.append(FactorSpec(f"momentum_{n}", "momentum", f"{n}日动量(Close - Close[N])", _momentum(n)))
    for n in (6, 12, 14, 24):
        specs.append(FactorSpec(f"rsi_{n}", "momentum", f"{n}日相对强弱指标(RSI)", _rsi(n)))
    specs.append(FactorSpec("macd", "momentum", "MACD 线(EMA12 - EMA26)", _macd()))
    specs.append(FactorSpec("macd_signal", "momentum", "MACD 信号线(9日 EMA of MACD)", _macd_signal()))
    specs.append(FactorSpec("macd_hist", "momentum", "MACD 柱(MACD - Signal)", _macd_hist()))

    # 波动率 — 8 个
    specs.append(FactorSpec("atr_14", "volatility", "14日 ATR(Average True Range, Wilder)", _atr(14)))
    for n in (5, 10, 20):
        specs.append(FactorSpec(f"std_{n}", "volatility", f"{n}日 Close 标准差", _std(n)))
    for n in (20, 60):
        specs.append(FactorSpec(f"histvol_{n}", "volatility", f"{n}日年化历史波动率(log ret × √252)", _histvol(n)))
    specs.append(FactorSpec("boll_pct_b", "volatility", "布林带 %b(Close 在布林带中的位置)", _boll(20, 2.0)))

    # 量价 — 8 个
    specs.append(FactorSpec("obv", "volume_price", "OBV(On-Balance Volume,累计)", _obv()))
    specs.append(FactorSpec("vpt", "volume_price", "VPT(Volume-Price Trend)", _vpt()))
    specs.append(FactorSpec("pvt", "volume_price", "PVT(Price-Volume Trend 归一)", _pvt()))
    specs.append(FactorSpec("vwap_5", "volume_price", "5日 VWAP", _vwap(5)))
    specs.append(FactorSpec("vwap_20", "volume_price", "20日 VWAP", _vwap(20)))
    specs.append(FactorSpec("volume_ratio_5", "volume_price", "量比(当日量 / 5日均量)", _volume_ratio(5)))

    # 趋势 — 5 个
    specs.append(FactorSpec("adx_14", "trend", "14日 ADX(Average Directional Index)", _adx(14)))
    specs.append(FactorSpec("cci_20", "trend", "20日 CCI(Commodity Channel Index)", _cci(20)))
    specs.append(FactorSpec("aroon_up_25", "trend", "25日 Aroon Up", _aroon_up(25)))
    specs.append(FactorSpec("aroon_down_25", "trend", "25日 Aroon Down", _aroon_down(25)))

    return {s.name: s for s in specs}


REGISTRY: Dict[str, FactorSpec] = _build_registry()

CATEGORIES = {
    "momentum": "动量类因子(收益率、相对强弱、MACD)",
    "volatility": "波动率类因子(ATR、波动幅度、布林带)",
    "volume_price": "量价类因子(OBV、VWAP、量比)",
    "trend": "趋势类因子(ADX、CCI、Aroon)",
}


def list_factors(category: Optional[str] = None) -> List[FactorSpec]:
    """列出所有可用因子,可选按类别过滤"""
    if category:
        if category not in CATEGORIES:
            raise ValueError(f"未知类别 {category!r},可选: {list(CATEGORIES.keys())}")
        return [s for s in REGISTRY.values() if s.category == category]
    return list(REGISTRY.values())


def get_spec(name: str) -> FactorSpec:
    if name not in REGISTRY:
        raise ValueError(f"未知因子 {name!r},可用: {sorted(REGISTRY.keys())}")
    return REGISTRY[name]


def compute_factor(df: pd.DataFrame, name: str) -> pd.Series:
    """计算单个因子。DataFrame 需含 Open/High/Low/Close/Volume 列。"""
    spec = get_spec(name)
    return spec.func(df)


def compute_factors(
    df: pd.DataFrame,
    names: List[str],
) -> pd.DataFrame:
    """批量计算因子。返回 DataFrame(columns=因子名,index 与 df 相同)。"""
    out = {}
    for name in names:
        out[name] = compute_factor(df, name)
    return pd.DataFrame(out, index=df.index)


# ---------------------------------------------------------------------------
# IC / IR 评估 — 衡量因子对未来收益的预测能力
# ---------------------------------------------------------------------------

def _rank(a: np.ndarray) -> np.ndarray:
    """将数组转为秩(rank),NaN 保留 NaN"""
    s = pd.Series(a)
    return s.rank(method="average").to_numpy()


def evaluate_factor(
    df: pd.DataFrame,
    name_or_series,  # str(因子名)或 pd.Series(已计算好的因子值)
    forward_days: int = 5,
) -> Dict[str, float]:
    """评估因子的预测能力。

    计算:
    - IC: 因子值与 forward N 日收益的 Pearson 相关系数
    - Rank IC: Spearman(秩相关)
    - IC positive ratio: 滚动 IC > 0 的占比
    - IR: IC 的均值 / IC 的标准差

    Returns:
        dict 含 ic, rank_ic, ic_std, ic_positive_ratio, ir, n_samples
    """
    if forward_days < 1:
        raise ValueError("forward_days 必须 >= 1")
    close = _require_close(df)
    if isinstance(name_or_series, str):
        factor = compute_factor(df, name_or_series)
    elif isinstance(name_or_series, pd.Series):
        factor = name_or_series
    else:
        raise TypeError(f"name_or_series 必须是 str 或 pd.Series,得到 {type(name_or_series)}")

    # 未来 N 日的对数收益(今天买入,N 天后卖)
    forward_ret = np.log(close.shift(-forward_days) / close)
    # 只取两者都有效的样本
    mask = factor.notna() & forward_ret.notna()
    f = factor[mask].to_numpy()
    r = forward_ret[mask].to_numpy()

    if len(f) < 30:
        return {
            "ic": float("nan"),
            "rank_ic": float("nan"),
            "ic_std": float("nan"),
            "ic_positive_ratio": float("nan"),
            "ir": float("nan"),
            "n_samples": int(len(f)),
        }

    # Pearson IC
    ic = float(np.corrcoef(f, r)[0, 1]) if f.std() > 0 and r.std() > 0 else float("nan")

    # Rank IC (Spearman)
    rf = _rank(f)
    rr = _rank(r)
    rank_ic = float(np.corrcoef(rf, rr)[0, 1]) if rf.std() > 0 and rr.std() > 0 else float("nan")

    # 滚动 IC(252 日窗口,月度评估)
    rolling_window = min(60, len(f) // 4)
    if rolling_window < 5:
        rolling_ic = np.array([ic])
    else:
        f_series = pd.Series(f)
        r_series = pd.Series(r)
        rolling_ic = f_series.rolling(rolling_window).corr(r_series).dropna().to_numpy()

    if len(rolling_ic) >= 2 and np.nanstd(rolling_ic) > 0:
        ic_std = float(np.nanstd(rolling_ic))
        ir = float(np.nanmean(rolling_ic) / ic_std) if ic_std > 0 else float("nan")
    else:
        ic_std = float("nan")
        ir = float("nan")

    positive_ratio = float(np.mean(rolling_ic > 0)) if len(rolling_ic) > 0 else float("nan")

    return {
        "ic": ic,
        "rank_ic": rank_ic,
        "ic_std": ic_std,
        "ic_positive_ratio": positive_ratio,
        "ir": ir,
        "n_samples": int(len(f)),
    }
