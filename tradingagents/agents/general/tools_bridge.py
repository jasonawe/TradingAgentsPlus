"""Stage C Agent 可用 tools 集合 — 8 个 LangChain @tool。

按 O7 选项 2 设计:
- Alpha158 × 3: list_alpha_factors / compute_alpha_factors / evaluate_alpha(直接复用 B1)
- Core × 5: get_quote / get_quotes_batch / get_history / get_fundamentals / list_watchlist

所有 tool 返回 str(LLM 友好),失败时返回 "ERROR: ..." 或 "NO_DATA: ..."
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool

# 复用 B1 的 alpha tools(已经实现好)
from tradingagents.agents.utils.alpha_factors_tools import (
    compute_alpha_factors,
    evaluate_alpha,
    list_alpha_factors,
)
# 复用 B0 的 fundamentals tool
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals as _get_fundamentals_raw,
)

from tradingagents.dataflows.stockstats_utils import load_ohlcv


# ════════════════════════════════════════════════════════
# 默认 DB 路径(L2/L3 共享 web_runs.sqlite3)
# ════════════════════════════════════════════════════════

def _default_db_path() -> Path:
    """L2/L3/watchlist 都在 web_runs.sqlite3。"""
    return Path.home() / ".tradingagents" / "web_runs.sqlite3"


# ════════════════════════════════════════════════════════
# Core Tool 1: get_quote(单个 symbol 实时报价)
# ════════════════════════════════════════════════════════

@tool
def get_quote(
    symbol: Annotated[str, "ticker symbol, 如 600036.SS / AAPL / 0700.HK"],
    asset_type: Annotated[str, "资产类型:stock/etf/crypto/idx,默认 stock"] = "stock",
) -> str:
    """获取单个 symbol 的实时报价。

    返回字段:price / change / change_percent / volume / open / high / low / currency / source / fetched_at
    """
    try:
        from web.market_data import QuoteService
        from web.config import resolve_model_config
        from tradingagents.default_config import DEFAULT_CONFIG

        # 简化的 service 初始化(实际可能需要 settings)
        service = QuoteService(DEFAULT_CONFIG)
        snap = service.get_quote(symbol, asset_type)
        if snap is None:
            return f"NO_DATA: 未能获取 {symbol!r} 的报价"

        # 格式化为 LLM 友好文本
        return (
            f"symbol: {snap.symbol}\n"
            f"price: {snap.price}\n"
            f"change: {snap.change}\n"
            f"change_percent: {snap.change_percent:.4f}\n"
            f"volume: {snap.volume}\n"
            f"open: {snap.open}\n"
            f"high: {snap.high}\n"
            f"low: {snap.low}\n"
            f"currency: {snap.currency}\n"
            f"source: {snap.source}\n"
            f"fetched_at: {snap.fetched_at.isoformat() if hasattr(snap.fetched_at, 'isoformat') else snap.fetched_at}\n"
        )
    except Exception as e:
        return f"ERROR: get_quote({symbol}) failed - {type(e).__name__}: {e}"


# ════════════════════════════════════════════════════════
# Core Tool 2: get_quotes_batch(批量报价,最多 20 个)
# ════════════════════════════════════════════════════════

@tool
def get_quotes_batch(
    symbols: Annotated[str, "逗号分隔 tickers, 最多 20 个,如 '600036.SS,AAPL,BABA'"],
    asset_type: Annotated[str, "资产类型,默认 stock"] = "stock",
) -> str:
    """批量查询报价(最多 20 个)。

    返回 markdown 表格,包含 symbol / price / change_percent / volume / source。
    """
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        return "ERROR: 必须至少指定一个 symbol"
    if len(sym_list) > 20:
        return f"ERROR: 批量最多 20 个 symbol,当前 {len(sym_list)} 个"

    try:
        from web.market_data import QuoteService
        from tradingagents.default_config import DEFAULT_CONFIG

        service = QuoteService(DEFAULT_CONFIG)
        bulk = service.get_quotes(sym_list, asset_type)  # BulkQuoteResponse
        quotes = bulk.items  # list[QuoteItem]

        if not quotes:
            return "NO_DATA: 所有 symbol 均未获取到报价"

        prefix = (
            f"⚠️ 部分 symbol 获取失败 ({len(sym_list) - len(quotes)}/{len(sym_list)})\n\n"
            if bulk.partial else ""
        )
        lines = [
            "| symbol | price | change_percent | volume | source |",
            "|---|---|---|---|---|",
        ]
        for q in quotes:
            cp = f"{q.change_percent:.4f}" if q.change_percent is not None else "N/A"
            lines.append(
                f"| {q.symbol} | {q.price} | {cp}% | {q.volume} | {q.source} |"
            )
        return prefix + "\n".join(lines)
    except Exception as e:
        return f"ERROR: get_quotes_batch failed - {type(e).__name__}: {e}"


# ════════════════════════════════════════════════════════
# Core Tool 3: get_history(K 线数据 + 简单技术指标)
# ════════════════════════════════════════════════════════

@tool
def get_history(
    symbol: Annotated[str, "ticker symbol"],
    period: Annotated[str, "回看周期:1mo/3mo/6mo/1y/2y/5y,默认 1y"] = "1y",
    interval: Annotated[str, "K线间隔:1d/1h/30m/15m/5m,默认 1d"] = "1d",
) -> str:
    """获取历史 K 线数据 + 简单技术指标摘要。

    用 B3 BarGenerator(支持多周期);内部走本地缓存。
    返回:最新 30 行 CSV + MA20/RSI14 摘要。
    """
    try:
        # B3 优先(B3 fetch_daily_candles 的 period 只接 1d/1w/1M);
        # 非日线 interval 走 fallback
        try:
            from web.bar_generator import fetch_daily_candles

            # period 参数(用户输入如 1y/3mo) → count(days)
            count_map = {
                "1mo": 22, "3mo": 65, "6mo": 130,
                "1y": 252, "2y": 504, "5y": 1260,
            }
            count = count_map.get(period, 252)

            # B3 限制:fetch_daily_candles 只支持日/周/月;其他 interval 走 fallback
            if interval in ("1d", "1w", "1M"):
                df = fetch_daily_candles(symbol, count=count, period=interval)
                source = f"web/bar_generator (B3, {interval})"
            else:
                # 非日线 interval 走 stockstats_utils
                today = datetime.utcnow().strftime("%Y-%m-%d")
                df = load_ohlcv(symbol, today).tail(count)
                source = (
                    f"stockstats_utils (interval={interval} 不支持多周期,"
                    f"取最近 {count} 日)"
                )
        except Exception:
            # 整体 fallback
            today = datetime.utcnow().strftime("%Y-%m-%d")
            df = load_ohlcv(symbol, today)
            source = "stockstats_utils (fallback)"

        if df is None or df.empty:
            return f"NO_DATA: 未能获取 {symbol!r} 的历史数据"

        # 取最近 30 行
        tail = df.tail(30).copy()

        # 简单技术指标
        closes = df["Close"] if "Close" in df.columns else df["close"]
        ma20 = closes.tail(20).mean()
        rsi14 = _compute_rsi(closes.tail(15))

        # 格式化为 CSV + 摘要
        csv = tail.to_csv(float_format="%.4f") if hasattr(tail, "to_csv") else str(tail)

        latest = tail.iloc[-1]
        return (
            f"symbol: {symbol}\n"
            f"period: {period}, interval: {interval}\n"
            f"source: {source}\n"
            f"rows: {len(df)} (showing last {len(tail)})\n"
            f"latest close: {latest.get('Close', latest.get('close', 'N/A'))}\n"
            f"latest date: {latest.get('Date', latest.get('date', 'N/A'))}\n"
            f"MA20: {ma20:.4f}\n"
            f"RSI14: {rsi14:.4f}\n"
            f"\n--- 最近 30 行 ---\n{csv}"
        )
    except Exception as e:
        return f"ERROR: get_history({symbol}) failed - {type(e).__name__}: {e}"


def _compute_rsi(series, period: int = 14) -> float:
    """Wilder 平滑 RSI(简化版)。"""
    import pandas as pd
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


# ════════════════════════════════════════════════════════
# Core Tool 4: get_fundamentals(复用 B0)
# ════════════════════════════════════════════════════════

@tool
def get_fundamentals(
    symbol: Annotated[str, "ticker symbol, 如 600036.SS / AAPL"],
    curr_date: Annotated[str, "当前交易日 yyyy-mm-dd,默认今天"] = "",
) -> str:
    """获取基本面数据(PE/PB/市值/ROE/营收等)。

    复用 tradingagents/agents/utils/fundamental_data_tools.get_fundamentals。
    """
    if not curr_date:
        curr_date = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        return _get_fundamentals_raw.invoke(
            {"ticker": symbol, "curr_date": curr_date}
        )
    except Exception as e:
        return f"ERROR: get_fundamentals({symbol}) failed - {type(e).__name__}: {e}"


# ════════════════════════════════════════════════════════
# Core Tool 5: list_watchlist(用户关注列表)
# ════════════════════════════════════════════════════════

@tool
def list_watchlist() -> str:
    """列出当前用户的关注列表(从 web_runs.sqlite3.watchlist_items 读)。

    返回 markdown 表格:symbol / asset_type / note / position / created_at。
    """
    db_path = _default_db_path()
    if not db_path.exists():
        return "NO_DATA: 数据库未初始化"

    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            """SELECT symbol, asset_type, note, position, created_at
               FROM watchlist_items
               WHERE watchlist_id = 'default'
               ORDER BY position ASC, created_at DESC"""
        ).fetchall()
        conn.close()

        if not rows:
            return "(用户关注列表为空)"

        lines = [
            f"共 {len(rows)} 个关注项:",
            "| symbol | asset_type | note | position | created_at |",
            "|---|---|---|---|---|",
        ]
        for sym, atype, note, pos, created in rows:
            note_s = (note or "")[:30]
            lines.append(f"| {sym} | {atype} | {note_s} | {pos} | {created} |")
        return "\n".join(lines)
    except Exception as e:
        return f"ERROR: list_watchlist failed - {type(e).__name__}: {e}"


# ════════════════════════════════════════════════════════
# ALL_TOOLS — LangGraph ReAct agent 直接用
# ════════════════════════════════════════════════════════

ALL_TOOLS = [
    # Alpha158 × 3(B1)
    list_alpha_factors,
    compute_alpha_factors,
    evaluate_alpha,
    # Core × 5(Stage C 新加)
    get_quote,
    get_quotes_batch,
    get_history,
    get_fundamentals,
    list_watchlist,
]


__all__ = [
    "ALL_TOOLS",
    "get_quote",
    "get_quotes_batch",
    "get_history",
    "get_fundamentals",
    "list_watchlist",
]
