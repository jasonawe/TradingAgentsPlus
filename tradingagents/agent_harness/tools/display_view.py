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


def _render_list(r: dict) -> str:
    """Generic list_X tools. Renders count + first N items."""
    count = r.get("count")
    text = r.get("text") or r.get("summary") or ""
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
    "alpha_list": _render_list,
}
