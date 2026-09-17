"""Single source of truth: tool result → 用户态中文短句。

All write-CRUD status → 中文短句的映射都集中在这里。前端 trace 渲染
有自己独立的 mapping(见 web/static/harness.js appendToolResult 的
STATUS_BADGE 表),但语义必须保持一致 — spec §10 审查清单明文要求。

Three call sites use this module:
1. ``_invoke_bridge._parse_bridge_text`` — attach ``summary`` to the
   parsed dict right after the bridge prefix is detected.
2. ``Orchestrator._trivial_crud_summary`` — batch-aggregate for the
   Tier 2 fast-path (wraps ``summarize_tool_results``).
3. ``/api/harness/sessions/{sid}/confirm`` — defensive fallback in case
   the tool layer didn't fill ``summary`` already.

Add a new entity? Update ``_ENTITY_PREFIXES`` AND ``_TEMPLATES``.
Add a new status? Update ``summarize_tool_result`` directly.
"""
from __future__ import annotations

import json as _json
from typing import Any

# Bridge raw prefixes that uniquely identify the entity + op.
# Mirror of ``_BRIDGE_PREFIXES`` in ``tools/builtin.py`` — kept here as
# the single source of truth for the *user-facing* mapping; the tool
# layer's copy drives parsing only.
_ENTITY_PREFIXES: dict[str, str] = {
    "NOTE_CREATED:": "note",
    "NOTE_UPDATED:": "note",
    "NOTE_DELETED:": "note",
    "ALERT_CREATED:": "alert",
    "ALERT_UPDATED:": "alert",
    "ALERT_DELETED:": "alert",
    "SCHEDULED_CREATED:": "scheduled",
    "SCHEDULED_UPDATED:": "scheduled",
    "SCHEDULED_DELETED:": "scheduled",
    "ADDED:": "watchlist",
    "REMOVED:": "watchlist",
    "DUPLICATE:": "watchlist",
}

# status × entity → 中文短句模板(无 symbol 占位符)
_TEMPLATES: dict[tuple[str, str], str] = {
    ("created", "note"): "笔记已保存",
    ("updated", "note"): "笔记已更新",
    ("deleted", "note"): "笔记已删除",
    ("created", "alert"): "告警已创建",
    ("updated", "alert"): "告警已更新",
    ("deleted", "alert"): "告警已删除",
    ("created", "scheduled"): "定时任务已创建",
    ("updated", "scheduled"): "定时任务已更新",
    ("deleted", "scheduled"): "定时任务已删除",
    ("created", "watchlist"): "已加入关注",
    ("deleted", "watchlist"): "已移出关注",
    ("duplicate", "watchlist"): "已在关注列表中,无需重复添加",
}


def _extract_symbol_from_raw(raw: str) -> str | None:
    """抠 raw JSON 里的 ``symbol`` 字段,作为 fallback。

    Bridge 字符串格式 ``PREFIX: {"id":..., "symbol":..., ...}`` — 抠
    ``:`` 后面的 JSON 解析。解析失败返回 None(由调用方决定 fallback)。
    """
    if not isinstance(raw, str) or ":" not in raw:
        return None
    try:
        payload = _json.loads(raw.split(":", 1)[1].strip())
    except Exception:
        return None
    return payload.get("symbol") if isinstance(payload, dict) else None


def _try_parse_embedded_summary(raw: str) -> str | None:
    """Bulk delete 工具把 summary 封在 raw JSON 里,抠出来直接用。

    ``delete_notes_for_symbol`` / ``delete_alerts_for_symbol`` 这类
    工具的 bridge 字符串本身就是 JSON:``{"status": "ok", "summary":
    "资产 X 的笔记已删除:N/N 条成功", ...}``。一旦命中,直接返回
    ``summary`` 字段 — 比 status-based templating 精确得多。

    Synthetic fallback: 当 bridge 没填 ``summary`` 但有
    ``matched/deleted/failed`` 字段(典型:
    ``delete_scheduled_tasks_for_symbol``),合成 "已删除:deleted/
    matched 条成功 [+ failed 条失败]" 的文案。
    """
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    try:
        payload = _json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("summary"), str):
        return payload["summary"]
    matched = payload.get("matched")
    deleted = payload.get("deleted")
    failed = payload.get("failed")
    if matched is None or deleted is None:
        return None
    symbol = payload.get("symbol")
    sym_part = f"({symbol}) " if symbol else ""
    if matched == 0:
        return f"{sym_part}当前没有可删除项".strip()
    base = f"{sym_part}已删除:{deleted}/{matched} 条成功"
    if failed:
        base += f",{failed} 条失败"
    return base


def summarize_tool_result(result: dict[str, Any]) -> str | None:
    """单条工具结果 → 用户态中文短句。

    Returns None when the result is not a trivial CRUD ack (caller
    should fall through to LLM synthesis / client-side formatRawResult).
    这是 backend / frontend 渲染层唯一允许走的地方(through this
    module) — 其他地方想要中文短句一律走这条路,不要重新发明。
    """
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    raw = result.get("raw") or ""
    symbol = result.get("symbol")  # 顶层 symbol 优先
    entity = None

    # 优先级 1: bulk delete 等工具自带 summary(封在 raw JSON 里)
    if status == "ok":
        embedded = _try_parse_embedded_summary(raw)
        if embedded:
            return embedded

    # 优先级 2: NOTE_*/ALERT_*/SCHEDULED_*/ADDED/REMOVED/DUPLICATE prefix
    if status in ("created", "updated", "deleted", "duplicate"):
        for prefix, ent in _ENTITY_PREFIXES.items():
            if prefix in raw:
                entity = ent
                if not symbol:
                    symbol = _extract_symbol_from_raw(raw)
                break
        if entity:
            tmpl = _TEMPLATES.get((status, entity))
            if tmpl:
                return f"{tmpl} ({symbol})" if symbol else tmpl

    # 优先级 3: 通用 ok 路径(PREFERENCE_UPDATED, etc.)
    # status=ok with data-bearing fields (price/items/factors/etc.) means
    # a real data payload — leave it for the LLM synthesizer or the
    # client-side formatRawResult. This mirrors
    # Orchestrator._trivial_crud_summary's pre-filter.
    if status == "ok" and any(result.get(k) for k in (
        "price", "items", "factors", "alerts", "notes", "rows"
    )):
        return None
    if status == "ok" and "PREFERENCE_UPDATED" in raw:
        return "偏好已更新"
    if status == "ok" and "added" in raw.lower():
        return f"{symbol or ''} 已加入关注".strip()
    # Generic ok fallback — back-compat with legacy _format_trivial_summary
    # (status=ok + plain/raw text + no data fields).
    if status == "ok":
        return "操作成功"

    # pending_approval → None(走前端 modal,不显示在 bubble)
    if status == "pending_approval":
        return None

    # 未知 status / 数据型 ok → None(留给 LLM synthesize 或客户端)
    return None


def summarize_tool_results(results: list[dict[str, Any]]) -> str:
    """批量:每条独立渲染,换行拼接。空字符串表示没有可 trivial 化的内容。"""
    parts: list[str] = []
    for r in results or []:
        s = summarize_tool_result(r)
        if s:
            parts.append(s)
    return "\n".join(parts)
