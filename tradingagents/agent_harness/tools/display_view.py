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


def display_view_for(result: Any, *, intent: str) -> str:
    """Render a friendly summary for ``result`` based on ``intent``.

    The mapping is intentionally explicit — adding a new intent is a
    one-line change here rather than a sprawling if/elif tree.
    Falls back to JSON when no template matches.
    """
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, default=str)

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
    sym = r.get("symbol", "?")
    price = r.get("price")
    price_str = f"{price:.2f}" if isinstance(price, (int, float)) else str(price)
    lines = [f"📈 {sym} · ¥{price_str}"]
    if isinstance(r.get("change"), (int, float)):
        sign = "+" if r["change"] >= 0 else ""
        ch = f"{sign}{r['change']:.2f}"
        if isinstance(r.get("change_pct"), (int, float)):
            ch += f" ({sign}{r['change_pct']:.2f}%)"
        lines.append(f"  涨跌: {ch}")
    if isinstance(r.get("volume"), (int, float)):
        lines.append(f"  成交量: {r['volume']:,}")
    return "\n".join(lines)


def _render_history(r: dict) -> str:
    sym = r.get("symbol", "?")
    candles = r.get("candles") or []
    last = candles[-1] if candles else {}
    last_line = (
        f"  最新: ¥{last.get('close'):.2f} @ {last.get('timestamp', '?')}"
        if last.get("close") is not None else ""
    )
    return "\n".join(filter(None, [
        f"📊 {sym} ({r.get('interval', '')}, {len(candles)} 根)",
        last_line,
    ]))


def _render_fundamentals(r: dict) -> str:
    sym = r.get("symbol", "?")
    lines = [f"💼 {sym}"]
    for k, label in [
        ("pe_ratio", "PE"),
        ("pb_ratio", "PB"),
        ("market_cap", "市值"),
        ("roe", "ROE"),
    ]:
        v = r.get(k)
        if v is not None:
            if isinstance(v, (int, float)):
                lines.append(f"  {label}: {v:.2f}" if abs(v) < 1e6 else f"  {label}: {int(v):,}")
            else:
                lines.append(f"  {label}: {v}")
    return "\n".join(lines)


def _render_news(r: dict) -> str:
    sym = r.get("symbol", "?")
    items = (r.get("items") or [])[:3]
    head = f"📰 {sym} ({len(r.get('items') or [])} 条)"
    if not items:
        return head
    body = "\n".join(f"  - {it.get('title', '(无标题)')}" for it in items)
    return f"{head}\n{body}"


def _render_ack(r: dict) -> str:
    """Friendly rendering for write tool acks.

    Mirrors the JS-side ``renderWriteAck`` so behaviour stays
    consistent between frontend and backend (e.g. for
    automated test fixtures that look at summary strings).
    """
    status = r.get("status")
    raw = r.get("raw") or ""
    verb_map = {
        "NOTE_CREATED": "笔记已创建",
        "NOTE_UPDATED": "笔记已更新",
        "NOTE_DELETED": "笔记已删除",
        "ALERT_CREATED": "告警已创建",
        "ALERT_UPDATED": "告警已更新",
        "ALERT_DELETED": "告警已删除",
        "WATCHLIST_ADDED": "已加入关注",
        "WATCHLIST_REMOVED": "已移除关注",
        "ADDED": "已加入关注",
        "REMOVED": "已移除关注",
        "DUPLICATE": "已存在(未重复添加)",
        "UPDATED": "已更新",
        "DELETED": "已删除",
        "CREATED": "已创建",
    }
    # Find a verb prefix in raw.
    label = None
    sym = ""
    for verb, lab in verb_map.items():
        if raw.startswith(verb):
            label = lab
            payload = raw[len(verb):].lstrip(": ").strip()
            if payload.startswith("{"):
                try:
                    parsed = json.loads(payload)
                    sym = parsed.get("symbol", "")
                except Exception:
                    pass
            elif payload and not payload.startswith("{"):
                sym = payload
            break
    if label is None:
        label = "已保存" if status in {"created", "updated", "deleted"} else "完成"
    return f"✅ {label} ({sym})" if sym else f"✅ {label}"


def _render_alpha(r: dict) -> str:
    """Render a compute_alpha_factors payload.

    Input shape:
        {"symbol": "600036.SS", "values": {"roc_1": 0.012, "rsi_14": 55.6, ...}}

    Layout:
        - Header with symbol + factor count
        - Markdown table (factor, value) limited to first 20 entries
          so the bubble stays scannable; full data lives in result_raw.
    """
    sym = r.get("symbol", "?")
    values = r.get("values") or {}
    if not isinstance(values, dict) or not values:
        return f"\u00b1 {sym} \u00b7 (no factor values)"
    head = f"± {sym} · {len(values)} 个因子最近读数"
    rows = list(values.items())[:20]
    # Each row MUST be a valid markdown table line: leading ``|``,
    # trailing ``|``, NO leading whitespace (otherwise some renderers
    # break out of the table).  Value is rounded to 6 dp so 32 factors
    # don't overflow the bubble.
    def _fmt(v):
        # Float with zero fractional part → print as int so huge
        # counts (-308733586.0) and tiny ints (1.0) don't get
        # 6dp-rendered into the bubble.
        if isinstance(v, float):
            if v.is_integer():
                return str(int(v))
            return f"{v:.6f}"
        return str(v)
    body = "\n".join(f"| `{k}` | {_fmt(v)} |" for k, v in rows)
    extra = ""
    if len(values) > 20:
        extra = f"\n| ... | (其他 {len(values) - 20} 个因子子见原始返回) |"
    return f"{head}\n| factor | value |\n|---|---|---|\n{body}{extra}"


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
