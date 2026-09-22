"""§0.4.13 — Tool routing hints & cheat sheet (Plan B + D).

Two complementary mechanisms to reduce LLM tool-selection errors
observed since the harness went live (2026-09 / §0.4.12 review):

Plan B — Per-intent routing hint injected into the planner's user
prompt (``_build_plan_prompt``). When the classifier pinned the user
message to a known :class:`Intent`, the planner now sees a one-line
routing hint that names the correct agent + tool and warns against
common wrong picks (e.g. "走势/N天" must NOT degrade to ``get_quote``
just because the user typed "价格").

Plan D — "Tool Routing Cheat Sheet" appended to the synthesizer
system prompt (``_SYNTH_SYSTEM``). SynthesizeNode reads it as a
few-shot anchor when tool_results cover multiple tools or when
intent was ambiguous at planning time.

Both pieces live here (rather than as string literals in
``orchestrator.py``) so future tweaks stay in one file and unit
tests can assert against the dictionary directly.
"""
from __future__ import annotations

from typing import Iterable

from .tier import Intent


# ----------------------------------------------------------------------
# Plan B — per-intent routing hint (injected into _build_plan_prompt)
# ----------------------------------------------------------------------
# The hint is one short Chinese sentence + a "do NOT pick …" warning
# for the failure mode the classifier is most likely to mis-fire on.
# Missing intent (Intent.UNKNOWN) gets a recovery paragraph that walks
# the LLM through the keyword heuristic.
_INTENT_ROUTING_HINTS: dict[Intent, str] = {
    Intent.QUOTE: (
        "本轮意图=quote (实时行情): 使用 data_agent 调用 get_quote 或 "
        "get_quotes_batch;不要换成 get_history / get_fundamentals / get_news。"
    ),
    Intent.HISTORY: (
        "本轮意图=history (历史走势 / N 天 K 线): 使用 data_agent 调用 get_history, "
        "并明确 time_range(如 args={symbol:..., time_range:'30d', interval:'1d'});"
        "不要用 get_quote (那只是当前快照,没有 K 线);get_history 返回多根 K 线数据。"
    ),
    Intent.FUNDAMENTALS: (
        "本轮意图=fundamentals (基本面): 使用 data_agent 调用 get_fundamentals;"
        "不要换成 get_quote (没财务数据);若用户问的是市值/PE/营收,都走它。"
    ),
    Intent.NEWS: (
        "本轮意图=news (新闻 / 公告): 使用 news_agent 调用 get_news;"
        "不要用 data_agent (没新闻数据)。"
    ),
    Intent.ALPHA: (
        "本轮意图=alpha (alpha158 因子): 使用 alpha_agent;有具体 ticker 时调用 "
        "compute_alpha_factors (真实数值),没有 ticker 时调用 list_alpha_factors "
        "(因子目录);不要走 data_agent。"
    ),
    Intent.COMPARE: (
        "本轮意图=compare (多标的对比): 使用 PTC 模式并行 data_agent 调用 "
        "get_quotes_batch(symbols=[...]) 或对每个 symbol 并发 get_quote;"
        "不要逐个串行 await。"
    ),
    Intent.ANALYSIS: (
        "本轮意图=analysis (综合分析): 优先 data_agent 拉数据 + news_agent 看新闻;"
        "若用户提到 report_id / '基于之前那份报告',先 list_reports/get_report "
        "拿历史,再决定是否启动新的 run_trading_agents_analysis (异步,代价大,默认不要启动)。"
    ),
    Intent.WATCHLIST: (
        "本轮意图=watchlist (关注列表 CRUD): 直接走 _CRUD_DISPATCH 列表,不在 plan 里;"
        "list_watchlist (只读) / add_to_watchlist (新增) / remove_from_watchlist (删除)。"
    ),
    Intent.NOTE: (
        "本轮意图=note (笔记 CRUD): 直接走 _CRUD_DISPATCH;list_notes (只读) / "
        "create_note / update_note / delete_note。"
    ),
    Intent.ALERT: (
        "本轮意图=alert (告警 CRUD): 直接走 _CRUD_DISPATCH;list_alerts (只读) / "
        "create_alert / update_alert / delete_alert。"
    ),
    Intent.SCHEDULED: (
        "本轮意图=scheduled (定时任务 CRUD): 直接走 _CRUD_DISPATCH;"
        "list_scheduled_tasks (只读) / create_scheduled_task / update / delete / run_scheduled_task。"
    ),
    Intent.RUN: (
        "本轮意图=run (分析任务状态查询): 使用 list_runs / get_analysis_status / "
        "cancel_analysis_run;不要启动新的 run_trading_agents_analysis (除非用户明确说 "
        "'跑一下' / '开始分析' / 're-run')。"
    ),
    Intent.REPORT: (
        "本轮意图=report (分析报告查询): 用户给了 report_id → get_report (拿完整报告);"
        "用户没说 id → list_reports (列表);若是 '用上次的报告再分析一遍' 这类诉求,先 "
        "list_reports 找 report_id,再考虑 run_trading_agents_analysis(based_on_report_id=...)。"
    ),
    Intent.UNKNOWN: (
        "本轮意图=unknown (无法分类): 按 ticker 数和关键词推断:有 ticker + 价格/涨跌 "
        "→ quote;ticker + N 天/走势 → history;ticker + 市盈率/营收 → fundamentals;"
        "ticker + 新闻 → news;多 ticker + 对比 → compare(用 PTC);若是 CRUD 关键字 "
        "(关注/笔记/告警/定时),留给 _CRUD_DISPATCH 处理。"
    ),
}


def routing_hint(intent):
    """Return the routing hint for ``intent`` (empty when UNKNOWN missing).

    Accepts both :class:`Intent` and ``intent.value`` (str) so callers
    that only have ``state.intent.value`` (after a JSON round-trip) still
    work. Returns the unknown fallback when the intent key isn't in the
    table (defensive — keeps the planner robust against Intent enum
    additions in the future).
    """
    if intent is None:
        return _INTENT_ROUTING_HINTS[Intent.UNKNOWN]
    if isinstance(intent, Intent):
        return _INTENT_ROUTING_HINTS.get(intent, _INTENT_ROUTING_HINTS[Intent.UNKNOWN])
    # str fallback
    try:
        return _INTENT_ROUTING_HINTS[Intent(intent)]
    except (ValueError, KeyError):
        return _INTENT_ROUTING_HINTS[Intent.UNKNOWN]


# ----------------------------------------------------------------------
# Plan D — Tool Routing Cheat Sheet (appended to _SYNTH_SYSTEM)
# ----------------------------------------------------------------------
# Anchors the synthesizer when tool_results cover multiple tools or
# when intent was ambiguous at planning time. Designed to be terse —
# the LLM only needs the keyword → tool mapping; it doesn't need
# full args schemas (those live in the tools' descriptions).
_TOOL_ROUTING_CHEAT_SHEET: str = (
    "\n\n## Tool Routing Cheat Sheet (§0.4.13 Plan D)\n"
    "When tool_results include data from several tools, use the "
    "keyword → tool mapping below to anchor your synthesis. "
    "Cite tools by exact name in the `资料来源` block.\n"
    "\n"
    "行情数据 (read):\n"
    "- 实时价/收盘/涨跌/最新数据/分时 → `get_quote` / `get_quotes_batch`\n"
    "- 走势/N 天 K 线/过去一月/历史行情 → `get_history`\n"
    "- 市盈率/市值/营收/EPS/基本面/财务 → `get_fundamentals`\n"
    "- 新闻/公告/舆情/最近消息 → `get_news`\n"
    "- alpha 因子/IC/RankIC(有 ticker) → `compute_alpha_factors`;因子目录(无 ticker) → `list_alpha_factors`\n"
    "\n"
    "CRUD 实体 (read + write, write 走 HITL):\n"
    "- 我的关注/自选股 → `list_watchlist`(读);`add_to_watchlist` / `remove_from_watchlist`(写)\n"
    "- 笔记/记事/备注 → `list_notes`(读);`create_note` / `update_note` / `delete_note`(写)\n"
    "- 告警/价格提醒/预警 → `list_alerts`(读);`create_alert` / `update_alert` / `delete_alert`(写)\n"
    "- 定时任务/计划任务 → `list_scheduled_tasks`(读);`create_scheduled_task` / `update_scheduled_task` / `delete_scheduled_task` / `run_scheduled_task`(写)\n"
    "\n"
    "分析与报告:\n"
    "- 分析报告列表 / 历史报告 → `list_reports`(列表,无 report_id)\n"
    "- 某份报告详情 / 完整 markdown → `get_report`(需 report_id)\n"
    "- 分析任务状态 / 进度 / 取消 → `list_runs` / `get_analysis_status` / `cancel_analysis_run`\n"
    "- 启动新分析 / 跑一遍 / 基于报告再分析 → `run_trading_agents_analysis`(异步,返回 run_id 后用 `get_analysis_status` 轮询)\n"
    "\n"
    "Pick the tool whose keyword best matches the user's verb in the "
    "original message. When intent was ambiguous, the carry-forward "
    "asset from the previous turn is your anchor."
)


def cheat_sheet():
    """Return the cheat-sheet block (always non-empty)."""
    return _TOOL_ROUTING_CHEAT_SHEET


def all_intents():
    """Iterate every Intent enum value (testing helper)."""
    return _INTENT_ROUTING_HINTS.keys()


__all__ = [
    "routing_hint",
    "cheat_sheet",
    "all_intents",
]
