"""Stage C Agent 可用 tools 集合 — 15 个 LangChain @tool。

按 O7 选项 2 设计 + Day 3 加 7 个写工具(HITL):
- Alpha158 × 3: list_alpha_factors / compute_alpha_factors / evaluate_alpha(直接复用 B1)
- Core 读 × 5: get_quote / get_quotes_batch / get_history / get_fundamentals / list_watchlist
- Write × 7: create_note / update_note / delete_note / create_alert / update_alert /
              delete_alert / update_preference(HITL 需要 user 确认)

所有 tool 返回 str(LLM 友好):
  - 读工具失败时返回 "ERROR: ..." 或 "NO_DATA: ..."
  - 写工具未批准时返回 "AWAITING_CONFIRMATION: {...json...}"
    前端监听这个 marker,弹 confirm dialog
  - 写工具已批准并成功后返回 "TOOL_CREATED: {...}" 等
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from typing import Annotated

from tradingagents.agents.general.approval import (
    consume_approval,
    is_approved,
)
from tradingagents.agents.general.audit import log_write
from tradingagents.agents.general.guardrails import (
    describe_impact,
    is_write_tool,
    validate_write_intent,
)
from tradingagents.agents.general.memory import agent_memory_db_path

# ════════════════════════════════════════════════════════
# Repository 注入(由 web/app.py 启动时 set_repositories() 调用)
# ════════════════════════════════════════════════════════

_repos: dict[str, Any] = {}
_repos_lock = threading.Lock()


def set_repositories(repos: dict[str, Any]) -> None:
    """注入 write tools 需要的 repositories(notes / alerts)。

    由 web/app.py 启动时调用,这样写 tool 才能直接访问 NoteRepository /
    AlertRepository 实例(跟 web API 用同一份,避免重复实现 CRUD)。
    """
    with _repos_lock:
        _repos.clear()
        _repos.update(repos)


def _get_repo(name: str) -> Any:
    with _repos_lock:
        if name not in _repos:
            raise RuntimeError(
                f"repository {name!r} 未注入 — 调用 set_repositories() 先注入。"
            )
        return _repos[name]


# ════════════════════════════════════════════════════════
# QuoteService 注入(由 web/app.py 启动时调用)
# ════════════════════════════════════════════════════════

_quote_service: Any = None


def set_quote_service(service: Any) -> None:
    """注入 web.app.state.market_service,get_quote / get_quotes_batch 才能用。

    QuoteService 需要 (router, repository, ...) 多个参数,工具里手动构造太脆弱,
    所以直接复用 web app 已经构造好的实例。
    """
    global _quote_service
    _quote_service = service


def _get_quote_service() -> Any:
    if _quote_service is None:
        raise RuntimeError(
            "QuoteService 未注入 — 调用 set_quote_service() 先注入。"
        )
    return _quote_service


# ════════════════════════════════════════════════════════
# Day 7 — ActiveRunner / Scheduler / News / ReportRepo 注入
# 让 LLM agent 能调度 TradingAgentsPlus 的核心能力
# ════════════════════════════════════════════════════════

_active_runner: Any = None
_scheduler_service: Any = None
_news_provider: Any = None
_report_history: Any = None


def set_active_runner(runner: Any) -> None:
    """注入 RunManager 实例,run_trading_agents_analysis + get_analysis_status 用。"""
    global _active_runner
    _active_runner = runner


def set_scheduler_service(svc: Any) -> None:
    """注入 ScheduledAnalysisService 实例,list_scheduled_tasks + run_scheduled_task 用。"""
    global _scheduler_service
    _scheduler_service = svc


def set_news_provider(provider: Any) -> None:
    """注入 news provider / function,get_news 用。
    provider 可以是 callable(ticker, start_date, end_date) 或 module。"""
    global _news_provider
    _news_provider = provider


def set_report_history(history: Any) -> None:
    """注入 ReportHistory 实例,list_reports 用。"""
    global _report_history
    _report_history = history


def _get_active_runner() -> Any:
    if _active_runner is None:
        raise RuntimeError("RunManager 未注入 — 调用 set_active_runner() 先注入。")
    return _active_runner


def _get_scheduler_service() -> Any:
    if _scheduler_service is None:
        raise RuntimeError("Scheduler 未注入 — 调用 set_scheduler_service() 先注入。")
    return _scheduler_service


def _get_news_provider() -> Any:
    if _news_provider is None:
        raise RuntimeError("News provider 未注入 — 调用 set_news_provider() 先注入。")
    return _news_provider


def _get_report_history() -> Any:
    if _report_history is None:
        raise RuntimeError("ReportHistory 未注入 — 调用 set_report_history() 先注入。")
    return _report_history

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
        service = _get_quote_service()
        snap = service.get_quote(symbol, asset_type)
        if snap is None:
            return f"NO_DATA: 未能获取 {symbol!r} 的报价"

        # 基础行情
        lines = [
            f"symbol: {snap.symbol}",
            f"price: {snap.price}",
            f"change: {snap.change}",
            f"change_percent: {snap.change_percent:.4f}" if snap.change_percent is not None else None,
            f"volume: {snap.volume}",
            f"open: {snap.open}",
            f"high: {snap.high}",
            f"low: {snap.low}",
            f"previous_close: {snap.previous_close}",
            f"currency: {snap.currency}",
            f"exchange: {snap.exchange}",
        ]
        # 量化指标(可能为 None,过滤掉)
        quant_fields = {
            "volume_ratio": snap.volume_ratio,
            "turnover": snap.turnover,
            "turnover_rate": snap.turnover_rate,
            "market_cap": snap.market_cap,
            "circulating_cap": snap.circulating_cap,
            "pe_ratio": snap.pe_ratio,
            "amplitude": snap.amplitude,
        }
        quant_lines = [
            f"  - {k}: {v}" for k, v in quant_fields.items() if v is not None
        ]
        if quant_lines:
            lines.append("quantitative_metrics:")
            lines.extend(quant_lines)

        lines.extend([
            f"source: {snap.source}",
            f"fetched_at: {snap.fetched_at.isoformat() if hasattr(snap.fetched_at, 'isoformat') else snap.fetched_at}",
            f"freshness: {snap.freshness}",
        ])
        return "\n".join(line for line in lines if line is not None)
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
        service = _get_quote_service()
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

# ════════════════════════════════════════════════════════
# Helper: 检查 approval + 包装写工具
# ════════════════════════════════════════════════════════

def _resolve_session_id(config: Any) -> str:
    """从 LangGraph RunnableConfig 提取 session_id(thread_id)。"""
    try:
        return config["configurable"]["thread_id"]
    except (KeyError, TypeError) as e:
        raise RuntimeError(
            f"无法从 config 提取 session_id: {type(e).__name__}: {e}"
        ) from e


def _check_write_approval(
    *,
    session_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """检查写操作是否已被 user 批准(单次)。

    Returns:
        None 表示已批准,可继续执行
        str 表示返回给 LLM 的 "AWAITING_CONFIRMATION: ..." 内容,
        LLM 不应该自己 retry,前端会发 confirm_request event
    """
    if is_approved(session_id, tool_name, tool_args):
        # 已被批准,允许执行(后续由具体工具 consume_approval 清除)
        return None

    # 未批准 → 返回 AWAITING_CONFIRMATION
    is_write, impact = validate_write_intent(tool_name, tool_args)
    payload = {
        "needs_confirmation": True,
        "tool_name": tool_name,
        "tool_args": tool_args,
        "impact": impact,
        "session_id": session_id,
    }
    # 同时写 audit log
    try:
        from web.config import resolve_model_config
        from tradingagents.default_config import DEFAULT_CONFIG
        # log_write 用 web_runs.sqlite3 默认路径
        audit_id = log_write(
            tool_name=tool_name,
            tool_args=tool_args,
            status="pending",
        )
        payload["audit_id"] = audit_id
    except Exception as e:  # pragma: no cover - defensive
        payload["audit_error"] = f"{type(e).__name__}: {e}"

    return (
        "AWAITING_CONFIRMATION: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _after_execute(
    session_id: str, tool_name: str, tool_args: dict[str, Any]
) -> None:
    """执行成功后消费 approval + 更新 audit。"""
    consume_approval(session_id, tool_name, tool_args)
    # audit 由具体工具处理(成功 / 失败分别 update)


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
# Core Tool 6-12: 写操作(7 个,都需要 HITL confirm)
# 设计:每个写 tool 都接收 session_id,通过 _check_write_approval 走 gate
# ════════════════════════════════════════════════════════

@tool
def create_note(
    symbol: Annotated[str, "ticker 如 600036.SS"],
    body_md: Annotated[str, "笔记 markdown 内容"],
    asset_type: Annotated[str, "stock / crypto,默认 stock"] = "stock",
    config: Annotated[RunnableConfig, InjectedToolArg()] = None,
) -> str:
    """为某资产创建笔记(需用户确认)。

    返回格式:
      - "NOTE_CREATED: {id: ..., symbol: ...}" 成功
      - "AWAITING_CONFIRMATION: {...}" 需要用户在前端确认
      - "ERROR: ..." 失败
    """
    session_id = _resolve_session_id(config)
    args = {"symbol": symbol, "body_md": body_md, "asset_type": asset_type}
    gate = _check_write_approval(
        session_id=session_id, tool_name="create_note", tool_args=args,
    )
    if gate is not None:
        return gate
    try:
        repo = _get_repo("notes")
        note = repo.create(symbol=symbol, body_md=body_md, asset_type=asset_type)
        _after_execute(session_id, "create_note", args)
        return (
            "NOTE_CREATED: "
            + json.dumps({"id": note.get("id"), "symbol": note.get("symbol")},
                         ensure_ascii=False)
        )
    except (ValueError, KeyError) as e:
        return f"ERROR: create_note failed - {e}"
    except Exception as e:
        return f"ERROR: create_note unexpected - {type(e).__name__}: {e}"


@tool
def update_note(
    note_id: Annotated[str, "笔记 ID,如 note-abc123..."],
    body_md: Annotated[str, "新内容(markdown)"],
    config: Annotated[RunnableConfig, InjectedToolArg()] = None,
) -> str:
    """修改现有笔记(需确认)。"""
    session_id = _resolve_session_id(config)
    args = {"note_id": note_id, "body_md": body_md}
    gate = _check_write_approval(
        session_id=session_id, tool_name="update_note", tool_args=args,
    )
    if gate is not None:
        return gate
    try:
        repo = _get_repo("notes")
        note = repo.update(note_id, body_md)
        _after_execute(session_id, "update_note", args)
        return (
            "NOTE_UPDATED: "
            + json.dumps({"id": note.get("id")}, ensure_ascii=False)
        )
    except (ValueError, KeyError) as e:
        return f"ERROR: update_note failed - {e}"
    except Exception as e:
        return f"ERROR: update_note unexpected - {type(e).__name__}: {e}"


@tool
def delete_note(
    note_id: Annotated[str, "笔记 ID"],
    config: Annotated[RunnableConfig, InjectedToolArg()] = None,
) -> str:
    """软删除笔记(需确认)。"""
    session_id = _resolve_session_id(config)
    args = {"note_id": note_id}
    gate = _check_write_approval(
        session_id=session_id, tool_name="delete_note", tool_args=args,
    )
    if gate is not None:
        return gate
    try:
        repo = _get_repo("notes")
        repo.soft_delete(note_id)
        _after_execute(session_id, "delete_note", args)
        return f"NOTE_DELETED: {note_id}"
    except KeyError as e:
        return f"ERROR: delete_note failed - {e}"
    except Exception as e:
        return f"ERROR: delete_note unexpected - {type(e).__name__}: {e}"


@tool
def create_alert(
    symbol: Annotated[str, "ticker 如 600036.SS"],
    kind: Annotated[str, "告警类型: price_above / price_below / change_pct / volume_spike 等"],
    params: Annotated[dict, "告警参数,如 {'threshold': 50.0}"],
    asset_type: Annotated[str, "stock / crypto,默认 stock"] = "stock",
    cooldown_seconds: Annotated[int, "触发冷却时间(秒),默认 3600"] = 3600,
    config: Annotated[RunnableConfig, InjectedToolArg()] = None,
) -> str:
    """创建价格 / 量化告警(需确认)。

    触发时通过飞书 / PushPlus webhook 通知。
    """
    session_id = _resolve_session_id(config)
    args = {
        "symbol": symbol,
        "kind": kind,
        "params": params,
        "asset_type": asset_type,
        "cooldown_seconds": cooldown_seconds,
    }
    gate = _check_write_approval(
        session_id=session_id, tool_name="create_alert", tool_args=args,
    )
    if gate is not None:
        return gate
    try:
        repo = _get_repo("alerts")
        alert = repo.create(
            symbol=symbol, asset_type=asset_type,
            kind=kind, params=params, cooldown_seconds=cooldown_seconds,
        )
        _after_execute(session_id, "create_alert", args)
        return (
            "ALERT_CREATED: "
            + json.dumps(
                {"id": alert.get("id"), "symbol": alert.get("symbol"),
                 "kind": alert.get("kind")},
                ensure_ascii=False,
            )
        )
    except (ValueError, KeyError) as e:
        return f"ERROR: create_alert failed - {e}"
    except Exception as e:
        return f"ERROR: create_alert unexpected - {type(e).__name__}: {e}"


@tool
def update_alert(
    alert_id: Annotated[str, "告警 ID,如 alert-abc123..."],
    enabled: Annotated[bool | None, "是否启用,None 表示不变"] = None,
    params: Annotated[dict | None, "新 params,None 表示不变"] = None,
    cooldown_seconds: Annotated[int | None, "新冷却时间,None 表示不变"] = None,
    config: Annotated[RunnableConfig, InjectedToolArg()] = None,
) -> str:
    """修改告警的启用状态 / 阈值 / 冷却(需确认)。"""
    session_id = _resolve_session_id(config)
    args = {
        "alert_id": alert_id,
        "enabled": enabled,
        "params": params,
        "cooldown_seconds": cooldown_seconds,
    }
    # 过滤 None 让 args 简洁
    args_clean = {k: v for k, v in args.items() if v is not None}
    gate = _check_write_approval(
        session_id=session_id, tool_name="update_alert", tool_args=args_clean,
    )
    if gate is not None:
        return gate
    try:
        repo = _get_repo("alerts")
        update_kwargs = {k: v for k, v in args_clean.items() if k != "alert_id"}
        alert = repo.update(alert_id, **update_kwargs)
        _after_execute(session_id, "update_alert", args_clean)
        return (
            "ALERT_UPDATED: "
            + json.dumps({"id": alert.get("id"), "enabled": alert.get("enabled")},
                         ensure_ascii=False)
        )
    except (ValueError, KeyError) as e:
        return f"ERROR: update_alert failed - {e}"
    except Exception as e:
        return f"ERROR: update_alert unexpected - {type(e).__name__}: {e}"


@tool
def delete_alert(
    alert_id: Annotated[str, "告警 ID"],
    config: Annotated[RunnableConfig, InjectedToolArg()] = None,
) -> str:
    """删除告警(软删除,需确认)。"""
    session_id = _resolve_session_id(config)
    args = {"alert_id": alert_id}
    gate = _check_write_approval(
        session_id=session_id, tool_name="delete_alert", tool_args=args,
    )
    if gate is not None:
        return gate
    try:
        repo = _get_repo("alerts")
        repo.soft_delete(alert_id)
        _after_execute(session_id, "delete_alert", args)
        return f"ALERT_DELETED: {alert_id}"
    except KeyError as e:
        return f"ERROR: delete_alert failed - {e}"
    except Exception as e:
        return f"ERROR: delete_alert unexpected - {type(e).__name__}: {e}"


@tool
def update_preference(
    key: Annotated[str, "偏好 key,如 default_provider / default_model"],
    value: Annotated[str, "偏好 value(JSON 序列化的 str)"],
    config: Annotated[RunnableConfig, InjectedToolArg()] = None,
) -> str:
    """修改用户偏好(影响后续 agent 行为,需确认)。

    value 必须是 JSON 序列化的字符串,如 '"akshare"' 或 '["a","b"]'。
    """
    session_id = _resolve_session_id(config)
    args = {"key": key, "value": value}
    gate = _check_write_approval(
        session_id=session_id, tool_name="update_preference", tool_args=args,
    )
    if gate is not None:
        return gate
    try:
        # L2 user_preferences 在 agent_memory_db 里
        from tradingagents.agents.general.memory import set_preference
        db_path = agent_memory_db_path(Path.home() / ".tradingagents")
        # value 需要 JSON parse 存进去
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        set_preference(db_path, key, parsed, source="agent")
        _after_execute(session_id, "update_preference", args)
        return f"PREFERENCE_UPDATED: {key}"
    except Exception as e:
        return f"ERROR: update_preference failed - {type(e).__name__}: {e}"


# ════════════════════════════════════════════════════════
# ALL_TOOLS — LangGraph ReAct agent 直接用
# ════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────
# Day 7 — 6 个新 tool,对接 TradingAgentsPlus 核心能力
# ─────────────────────────────────────────────────────

@tool
def run_trading_agents_analysis(
    symbol: Annotated[str, "ticker 如 600036.SS"],
    trade_date: Annotated[str, "YYYY-MM-DD;空 = 今天"] = "",
    asset_type: Annotated[str, "stock / crypto"] = "stock",
    research_depth: Annotated[int, "1 / 3 / 5 轮辩论深度"] = 1,
) -> str:
    """启动 TradingAgents 主图跑完整 pipeline(基本面/市场/新闻/辩论/风险管理)。

    同步返回 run_id + 初始状态,不阻塞等结果。
    想查进度用 get_analysis_status(run_id),完成后看 list_reports(symbol=...)。

    返回示例: {"status": "started", "run_id": "run-abc123", "symbol": "600036.SS", ...}
    """
    from datetime import date as _date
    from web.models import AnalysisRequest, AssetType, AnalystType
    runner = _get_active_runner()
    try:
        if trade_date:
            target = _date.fromisoformat(trade_date)
        else:
            target = _date.today()
    except ValueError:
        return f"ERROR: trade_date {trade_date!r} 必须是 YYYY-MM-DD"
    try:
        atype = AssetType(asset_type)
    except ValueError:
        return f"ERROR: asset_type {asset_type!r} 必须是 stock / crypto"
    if research_depth not in (1, 3, 5):
        research_depth = 1
    try:
        req = AnalysisRequest(
            ticker=symbol,
            analysis_date=target,
            asset_type=atype,
            analysts=[AnalystType.MARKET, AnalystType.FUNDAMENTALS],
            research_depth=research_depth,
        )
        record = runner.start_run(req)
        run_id = getattr(record, "run_id", None) or getattr(record, "id", None)
        return json.dumps(
            {
                "status": "started",
                "run_id": run_id,
                "symbol": symbol,
                "trade_date": target.isoformat(),
                "research_depth": research_depth,
                "hint": "调 get_analysis_status(run_id) 查进度,完成后用 list_reports 拿报告",
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return f"ERROR: run_trading_agents_analysis - {type(e).__name__}: {e}"


@tool
def get_analysis_status(run_id: Annotated[str, "run ID,run_trading_agents_analysis 返回"]) -> str:
    """查 run 当前状态(pending / running / completed / failed)+ 进度。"""
    runner = _get_active_runner()
    try:
        record = runner.get_run(run_id)
        return json.dumps(
            {
                "run_id": run_id,
                "status": getattr(record, "status", "unknown"),
                "ticker": getattr(getattr(record, "request", None), "ticker", None),
                "queued_at": str(getattr(record, "queued_at", None)),
                "started_at": str(getattr(record, "started_at", None)),
                "finished_at": str(getattr(record, "finished_at", None)),
                "error": getattr(record, "error", None),
                "hint": "status=completed 后调 list_reports 拿报告;status=failed 看 error 字段",
            },
            ensure_ascii=False,
            default=str,
        )
    except Exception as e:
        return f"ERROR: get_analysis_status - {type(e).__name__}: {e}"


@tool
def get_news(
    symbol: Annotated[str, "ticker 如 600036.SS"],
    days: Annotated[int, "查最近 N 天的新闻,默认 7"] = 7,
    asset_type: Annotated[str, "stock / crypto"] = "stock",
) -> str:
    """拿某资产的新闻(最近 N 天)。返回标题/来源/时间/摘要/sentiment 评分。"""
    from datetime import date as _date, timedelta as _td
    provider = _get_news_provider()
    end = _date.today()
    start = end - _td(days=max(1, min(days, 30)))
    try:
        # provider 可能是 callable / object.get_news / module.get_news
        if callable(provider):
            result = provider(symbol, start.isoformat(), end.isoformat())
        elif hasattr(provider, "get_news"):
            result = provider.get_news(symbol, start.isoformat(), end.isoformat())
        elif hasattr(provider, "get_stock_news"):
            result = provider.get_stock_news(symbol, start.isoformat(), end.isoformat())
        else:
            return "ERROR: news provider 接口未识别(需要 callable / .get_news / .get_stock_news)"
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, ensure_ascii=False, default=str)
        # 截断(避免 LLM context 爆掉)
        if len(text) > 4000:
            text = text[:4000] + "..."
        return text or "(no news)"
    except Exception as e:
        return f"ERROR: get_news - {type(e).__name__}: {e}"


@tool
def list_scheduled_tasks() -> str:
    """列出所有定时分析任务(每条包含 id / symbol / cron / 上次/下次运行时间 / enabled)。"""
    svc = _get_scheduler_service()
    try:
        result = svc.list_jobs()
        return json.dumps(result, ensure_ascii=False, default=str) or "(no scheduled tasks)"
    except Exception as e:
        return f"ERROR: list_scheduled_tasks - {type(e).__name__}: {e}"


@tool
def run_scheduled_task(job_id: Annotated[str, "list_scheduled_tasks 返回的 job id"]) -> str:
    """立即触发一个定时任务(不等 cron)。返回触发状态 + run_id(如有)。"""
    svc = _get_scheduler_service()
    try:
        result = svc.run_now(job_id)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return f"ERROR: run_scheduled_task - {type(e).__name__}: {e}"


@tool
def list_reports(
    symbol: Annotated[str, "ticker 过滤,空 = 全部"] = "",
    limit: Annotated[int, "最多返回几条,默认 20"] = 20,
) -> str:
    """列出历史分析报告(symbol 可选过滤)。返回 [{report_id, ticker, status, started_at, ...}]。"""
    history = _get_report_history()
    try:
        records = history.list_reports()
        if symbol:
            target = symbol.strip().upper()
            records = [r for r in records if str(r.get("ticker", "")).upper() == target]
        records = records[: max(1, min(limit, 200))]
        return json.dumps(records, ensure_ascii=False, default=str)
    except Exception as e:
        return f"ERROR: list_reports - {type(e).__name__}: {e}"


# 写 tool 列表 — 之前由 create_note / update_note / delete_note / create_alert /
# update_alert / delete_alert / update_preference 构成。
# 现在 ALL_TOOLS 已经包含读 + alpha + 写 + Day 7 共 21 个。

ALL_TOOLS = [
    # Alpha158 × 3(B1 复用)
    list_alpha_factors,
    compute_alpha_factors,
    evaluate_alpha,
    # Core 读 × 5(Stage C 新加)
    get_quote,
    get_quotes_batch,
    get_history,
    get_fundamentals,
    list_watchlist,
    # Write × 7(HITL,Day 3 新加 — 7 个写工具都要用户确认)
    create_note,
    update_note,
    delete_note,
    create_alert,
    update_alert,
    delete_alert,
    update_preference,
    # Day 7 — 6 个新 tool 对接 TradingAgentsPlus 核心能力
    run_trading_agents_analysis,
    get_analysis_status,
    get_news,
    list_scheduled_tasks,
    run_scheduled_task,
    list_reports,
]


__all__ = [
    "ALL_TOOLS",
    "set_repositories",
    "get_quote",
    "get_quotes_batch",
    "get_history",
    "get_fundamentals",
    "list_watchlist",
    "create_note",
    "update_note",
    "delete_note",
    "create_alert",
    "update_alert",
    "delete_alert",
    "update_preference",
    # Day 7
    "run_trading_agents_analysis",
    "get_analysis_status",
    "get_news",
    "list_scheduled_tasks",
    "run_scheduled_task",
    "list_reports",
    "set_active_runner",
    "set_scheduler_service",
    "set_news_provider",
    "set_report_history",
]
