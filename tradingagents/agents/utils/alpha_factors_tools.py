"""
Alpha158 因子工具集 — 暴露给 LangChain agent 使用。

Tools:
- list_alpha_factors: 列出所有可用因子及描述
- compute_alpha_factors: 计算指定 symbol 在指定时间的因子值
- evaluate_alpha: 评估因子的 IC/IR 表现(衡量预测能力)

数据获取:复用 stockstats_utils.load_ohlcv,有 5 年本地缓存。
"""

from __future__ import annotations

from typing import Annotated, List, Optional

import numpy as np
import pandas as pd
from langchain_core.tools import tool

from tradingagents.dataflows.alpha_factors import (
    CATEGORIES,
    compute_factors,
    evaluate_factor,
    list_factors,
)
from tradingagents.dataflows.stockstats_utils import load_ohlcv


# ---------------------------------------------------------------------------
# Tool 1: 列出因子
# ---------------------------------------------------------------------------

@tool
def list_alpha_factors(
    category: Annotated[Optional[str], "可选类别过滤:momentum / volatility / volume_price / trend"] = None,
) -> str:
    """列出所有可用的 alpha158 因子。

    返回按类别分组的因子清单,含因子名、类别、描述。
    当不确定用哪些因子时先调用此工具。

    Args:
        category: 可选类别过滤(momentum/volatility/volume_price/trend)。None 表示全部。
    """
    factors = list_factors(category)
    if not factors:
        return f"未找到类别 {category!r} 的因子"

    lines = [f"共 {len(factors)} 个 alpha158 因子:"]
    by_cat: dict[str, list] = {}
    for f in factors:
        by_cat.setdefault(f.category, []).append(f)

    for cat, specs in by_cat.items():
        cat_desc = CATEGORIES.get(cat, "")
        lines.append(f"\n## {cat}({cat_desc})")
        for s in specs:
            lines.append(f"  - `{s.name}`: {s.description}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 2: 计算因子值
# ---------------------------------------------------------------------------

@tool
def compute_alpha_factors(
    symbol: Annotated[str, "ticker symbol, 如 600036.SS / AAPL"],
    factors: Annotated[str, "逗号分隔的因子名,如 'roc_5,rsi_14,macd';也可用类别 macro 表示同类别所有因子"],
    curr_date: Annotated[str, "当前交易日 yyyy-mm-dd;因子值截止到该日(防 look-ahead)"],
    lookback_days: Annotated[int, "回看天数,默认 252(一年);范围 60-1260"] = 252,
) -> str:
    """计算指定 symbol 在指定时间的 alpha158 因子值。

    内部拉取 5 年 OHLCV(带本地缓存),按需计算所选因子。

    Args:
        symbol: ticker,如 600036.SS / AAPL。
        factors: 逗号分隔因子名,如 'roc_5,rsi_14,macd'。
        curr_date: 当前交易日 yyyy-mm-dd。
        lookback_days: 回看天数,默认 252。
    """
    # 解析 factors 字符串
    requested = [f.strip() for f in factors.split(",") if f.strip()]
    if not requested:
        return "ERROR: 必须至少指定一个因子"

    # 类别宏替换(macro 不可用,改为 'all:<category>')
    expanded: list[str] = []
    for token in requested:
        if token.startswith("all:"):
            cat = token.split(":", 1)[1].strip()
            if cat not in CATEGORIES:
                return f"ERROR: 未知类别 {cat!r},可选 {list(CATEGORIES.keys())}"
            expanded.extend([s.name for s in list_factors(cat)])
        else:
            expanded.append(token)

    # 去重但保持顺序
    seen = set()
    factor_names: list[str] = []
    for n in expanded:
        if n not in seen:
            factor_names.append(n)
            seen.add(n)

    # 校验
    try:
        df_full = load_ohlcv(symbol, curr_date)
    except Exception as e:
        return f"ERROR: 拉取 OHLCV 失败 - {e}"

    if df_full.empty or "Close" not in df_full.columns:
        return f"NO_DATA_AVAILABLE: 未找到 {symbol!r} 的行情数据"

    # 只保留 lookback_days
    df_full = df_full.tail(lookback_days + 10)  # 多取 10 行给 warmup

    try:
        factor_df = compute_factors(df_full, factor_names)
    except ValueError as e:
        return f"ERROR: {e}"

    # 过滤掉前 N 行的 NaN(因子 warmup 期)
    valid = factor_df.dropna(how="all")
    if valid.empty:
        return f"NO_DATA_AVAILABLE: 所选因子 {factor_names} 在 {symbol!r} 上无有效值(可能需要更长历史)"

    # 输出最近 30 行 + 简单汇总
    tail = valid.tail(30)
    # 把 index 转成日期字符串
    if "Date" in df_full.columns:
        tail = tail.copy()
        tail.index = pd.to_datetime(df_full["Date"].iloc[-len(tail):].values).strftime("%Y-%m-%d")

    csv = tail.to_csv(float_format="%.6f")

    # 汇总行数
    n_total = len(valid)
    latest = valid.iloc[-1]

    # 计算日期范围
    if "Date" in df_full.columns:
        date_series = pd.to_datetime(df_full["Date"]).reset_index(drop=True)
        valid_positions = np.where(valid.notna().any(axis=1).values)[0]
        date_min = date_series.iloc[valid_positions[0]].strftime("%Y-%m-%d") if len(valid_positions) > 0 else "N/A"
        date_max = date_series.iloc[valid_positions[-1]].strftime("%Y-%m-%d") if len(valid_positions) > 0 else "N/A"
        date_latest = date_series.iloc[-1].strftime("%Y-%m-%d")
    else:
        date_min = date_max = date_latest = "N/A"

    summary_lines = [
        f"symbol: {symbol}",
        f"factors: {', '.join(factor_names)}",
        f"date range: {date_min} ~ {date_max}",
        f"valid rows: {n_total}",
        f"latest values ({date_latest}):",
    ]
    for name in factor_names:
        v = latest.get(name)
        if pd.notna(v):
            summary_lines.append(f"  {name}: {v:.6f}")
        else:
            summary_lines.append(f"  {name}: NaN")

    return "\n".join(summary_lines) + "\n\n--- 最近 30 日 ---\n" + csv


# ---------------------------------------------------------------------------
# Tool 3: 评估因子 IC/IR
# ---------------------------------------------------------------------------

@tool
def evaluate_alpha(
    symbol: Annotated[str, "ticker symbol"],
    factor: Annotated[str, "因子名,如 'roc_5' 或 'rsi_14'"],
    curr_date: Annotated[str, "当前交易日 yyyy-mm-dd"],
    forward_days: Annotated[int, "前瞻天数,默认 5"] = 5,
) -> str:
    """评估因子的预测能力:IC、Rank IC、IC positive ratio、IR。

    Args:
        symbol: ticker。
        factor: 单个因子名。
        curr_date: 当前交易日。
        forward_days: 前瞻天数(评估因子对该天数收益的预测能力),默认 5。
    """
    try:
        df_full = load_ohlcv(symbol, curr_date)
    except Exception as e:
        return f"ERROR: 拉取 OHLCV 失败 - {e}"

    if df_full.empty or "Close" not in df_full.columns:
        return f"NO_DATA_AVAILABLE: 未找到 {symbol!r} 的行情数据"

    try:
        result = evaluate_factor(df_full, factor, forward_days=forward_days)
    except ValueError as e:
        return f"ERROR: {e}"

    if result["n_samples"] < 30:
        return (
            f"sample 不足({result['n_samples']}),无法评估 {symbol!r} 上的 {factor!r}。"
            f"建议使用更长的历史或换不同因子。"
        )

    def _fmt(v: float) -> str:
        if np.isnan(v):
            return "N/A"
        return f"{v:.4f}"

    lines = [
        f"因子评估: {factor} on {symbol}",
        f"前瞻天数: {forward_days}",
        f"样本数: {result['n_samples']}",
        "",
        f"  Pearson IC:    {_fmt(result['ic'])}",
        f"  Rank IC:       {_fmt(result['rank_ic'])}",
        f"  IC 标准差:     {_fmt(result['ic_std'])}",
        f"  IC 正向比率:   {_fmt(result['ic_positive_ratio'])}",
        f"  IR(IC 均值/标准差): {_fmt(result['ir'])}",
        "",
        "解读:",
        "  IC > 0.03 通常认为因子有弱预测能力",
        "  IC > 0.05 中等预测能力",
        "  IC > 0.10 强预测能力(实际中很罕见)",
        "  IC positive ratio > 0.55 表示因子在多数月份稳定正向",
    ]
    return "\n".join(lines)
