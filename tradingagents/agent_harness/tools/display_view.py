"""Step 23 — display_view helper.

When a tool result lands in the chat bubble the frontend wants a
short, human-readable summary — not the raw JSON dump. This helper
takes a result payload + intent string and picks the right template.

The renderers are intentionally simple (string templates, not Jinja)
because they run on every tool call and we don't want extra
plumbing for what is fundamentally a formatting layer.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .capabilities import Capability


from tradingagents.agent_harness.renderers.history_sparkline import render_history_card
from tradingagents.agent_harness.renderers.friendly_cards import (
    render_quote_card, render_fundamentals_card,
    render_news_card, render_alpha_card, render_ack_card,
    render_error_card,
)


def display_view_for(result: Any, *, intent: str) -> str:
    """Render a friendly summary for ``result`` based on ``intent``.

    The mapping is intentionally explicit — adding a new intent is a
    one-line change here rather than a sprawling if/elif tree.
    Falls back to JSON when no template matches.
    """
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, default=str)

    # §0.4.22.fix — short-circuit on error / no_data so the user gets a
    # friendly "⚠️ get_history: 暂无数据" card instead of a raw
    # ``❌ tool: no_data: historical candles unavailable`` text dump.
    # The error can live on the top-level result (most common) or nested
    # under ``result.error`` (some pipelines).
    err = result.get("error")
    err_code = result.get("error_code") or result.get("code")
    if err or (err_code and err_code != "ok"):
        return render_error_card(result, tool_name=intent)

    renderer = _RENDERERS.get(intent)
    if renderer is None:
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    try:
        return renderer(result)
    except Exception:
        # Renderer should never crash the agent bubble — fall back
        # to JSON so the user still sees *something*.
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def _render_quote(r: dict) -> str:
    """§0.4.22 — unified quote card."""
    return render_quote_card(r)




def _render_history(r: dict) -> str:
    """§0.4.17 — SVG sparkline + 关键指标 + 可折叠价格表."""
    return render_history_card(r)


def _render_fundamentals(r: dict) -> str:
    """§0.4.22 — unified fundamentals card."""
    return render_fundamentals_card(r)




def _render_news(r: dict) -> str:
    """§0.4.22 — unified news card."""
    return render_news_card(r)




def _render_ack(r: dict) -> str:
    """§0.4.22 — unified ack card."""
    return render_ack_card(r)




def _render_alpha(r: dict) -> str:
    """§0.4.22 — unified alpha card."""
    return render_alpha_card(r)




def _render_report_read(r: dict) -> str:
    """Parse the get_report ``text`` blob and render meta + content
    as a friendly markdown report card.

    The tool returns a single ``text`` string shaped like::

        REPORT: <report_id>
        ---meta---
        {"ticker": "...", "signal": "...", "status": "completed", ...}
        ---content---
        <full complete_report.md markdown body>

    Without this renderer, ``display_view_for`` falls back to JSON
    dump and the user sees a wall of escaped JSON in the chat bubble
    ("{status: ok, text: REPORT: run-...\\n---meta---\\{...\\}...").
    """
    raw = (
        r.get("text")
        or r.get("preview")
        or r.get("summary")
        or ""
    )
    if not isinstance(raw, str) or not raw.strip():
        return "(空)"
    # Split on the section markers. The impl writes them in this
    # exact order; tolerate leading whitespace and case variants.
    report_id = None
    meta_block = None
    content_block = raw
    m = re.match(r"^\s*REPORT:\s*(\S+)\s*\n", raw)
    if m:
        report_id = m.group(1)
        rest = raw[m.end():]
    else:
        rest = raw
    # Slice out the meta block if present.
    meta_match = re.search(
        r"^---meta---\s*\n(.*?)(?:\n---content---\s*\n|\Z)",
        rest, flags=re.DOTALL,
    )
    if meta_match:
        meta_block = meta_match.group(1).strip()
        after = rest[meta_match.end():]
        # If the impl used ---content--- to split, ``after`` already
        # starts at the content. Otherwise treat the whole tail as
        # content (legacy format).
        if after.startswith("---content---"):
            content_block = after[len("---content---"):].lstrip("\n")
        else:
            content_block = after.lstrip("\n")
    else:
        # No meta separator — the entire raw text is the report body.
        content_block = rest.lstrip("\n")
    # Try to pretty-print meta as a 2-column table.
    meta_md = ""
    if meta_block:
        try:
            meta_obj = json.loads(meta_block)
            if isinstance(meta_obj, dict) and meta_obj:
                rows = []
                # Friendly Chinese labels for the common keys.
                label_map = {
                    "ticker": "标的",
                    "asset_type": "类型",
                    "signal": "信号",
                    "rating": "评级",
                    "status": "状态",
                    "generated_at": "生成时间",
                    "analysis_date": "分析日期",
                    "source": "来源",
                    "report_id": "报告 ID",
                    "run_id": "运行 ID",
                }
                # Stable order: signal/rating first, then dates, then
                # everything else. Avoid making the user scroll a 30-row
                # table for a meta block with internal-only fields.
                priority_keys = [
                    "ticker", "signal", "rating", "status", "asset_type",
                    "analysis_date", "generated_at", "source",
                ]
                seen = set()
                for k in priority_keys:
                    if k in meta_obj:
                        rows.append((label_map.get(k, k), meta_obj[k]))
                        seen.add(k)
                for k, v in meta_obj.items():
                    if k in seen:
                        continue
                    # Skip nested dicts / lists — they're internal
                    # payloads (e.g. ``analysts: {market: "...long
                    # text...", news: "..."}``) that already live in
                    # the report's ``---content---`` body. Showing
                    # them here just dumps a JSON blob that obscures
                    # the actual meta. Only scalar fields go in the
                    # meta table.
                    if isinstance(v, (dict, list)):
                        continue
                    rows.append((label_map.get(k, k), v))
                rows_md = "\n".join(
                    f"| {k} | `{v if isinstance(v, (int, float, str, bool)) else str(v)}` |"
                    for k, v in rows
                )
                meta_md = (
                    "### 元数据\n"
                    "| 字段 | 值 |\n|---|---|\n" + rows_md + "\n\n"
                )
            else:
                meta_md = f"### 元数据\n\n```\n{meta_block}\n```\n\n"
        except Exception:
            # Not valid JSON — show as code fence.
            meta_md = f"### 元数据\n\n```\n{meta_block}\n```\n\n"
    head_md = f"## 📑 报告 {report_id}\n\n" if report_id else "## 📑 报告\n\n"
    return head_md + meta_md + content_block


def _render_list(r: dict) -> str:
    """Generic list_X tools. Renders count + the rich preview body.

    Step 8 / §Date — when a ListXResult has ``display_view()``, the
    orchestrator's ``_safe_dump`` projects the payload down to
    ``{summary, preview, count}``. The rich body (markdown table,
    itemised list, ...) lives in ``preview``; the ``summary`` field is
    just a one-liner like "1 条记录". Reading only ``text``/``summary``
    here used to lose the table — bubble showed just "共 1 条\n1 条
    记录" while the user was really asking for the table. Fall back
    through ``preview`` so list/notes/alerts/reports/etc. all show
    their full content.
    """
    count = r.get("count")
    text = (
        r.get("text")
        or r.get("preview")
        or r.get("summary")
        or ""
    )
    if isinstance(count, int):
        head = f"共 {count} 条"
        if text:
            return f"{head}\n{text}"
        return head
    return text or "(空)"


_RENDERERS = {
    Capability.QUOTE.value: _render_quote,
    Capability.HISTORY.value: _render_history,
    Capability.FUNDAMENTALS.value: _render_fundamentals,
    Capability.NEWS.value: _render_news,
    # CRUD writes share the ack renderer
    "ack": _render_ack,
    "list": _render_list,
    "watchlist_list": _render_list,
    "note_list": _render_list,
    "alert_list": _render_list,
    "scheduled_list": _render_list,
    "run_list": _render_list,
    "report_list": _render_list,
    "report_read": _render_report_read,
    "alpha_list": _render_list,
    "alpha": _render_alpha,
}
