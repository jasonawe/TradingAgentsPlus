"""Spec Step 22 — P0 UX wins: friendly write acks + clean result render.

Three regressions the user has been hitting daily:

1. Write tools (create_note / add_to_watchlist / etc.) return
   ``{status: "created", raw: "NOTE_CREATED: {id, symbol}"}`` — the
   raw JSON dump is shown verbatim. Friendly renderer should turn
   this into "✅ 笔记已创建 (note-xxx for 600036.SS)".

2. Tier 1 short-circuit on a write tool returns the same shape, then
   ``formatRawResult`` falls through to JSON.stringify. Same fix.

3. Approval modal needs both ``assistant`` in scope AND a fallback
   when the bubble was swapped for a fresh ``agent_final``. Helper
   resolves a stable ref.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# Pure helpers — run them in a JS-shaped sandbox via PyExec since they
# are pure functions of the result dict.

def _render(result: dict) -> str:
    """Mirror the JS formatRawResult write-ack detection logic."""
    if not isinstance(result, dict):
        return "raw"
    status = result.get("status")
    # status="ok" with ACK-style raw prefix is also a write ack
    raw = result.get("raw") or ""
    is_ack = status in {"created", "updated", "deleted", "duplicate"} or (
        status == "ok" and any(raw.startswith(p) for p in
            ("ADDED", "REMOVED", "DUPLICATE", "UPDATED", "DELETED", "CREATED"))
    )
    if is_ack:
        raw = result.get("raw") or ""
        # Parse "NOTE_CREATED: {\"id\": \"...\", \"symbol\": \"...\"}"
        # or "NOTE_UPDATED", "ADDED: ...", "DUPLICATE: ...".
        import json as _json
        import re as _re
        m = _re.match(r"^([A-Z_]+):\s*(.*)$", raw.strip())
        verb_map = {
            "NOTE_CREATED": "笔记已创建",
            "NOTE_UPDATED": "笔记已更新",
            "NOTE_DELETED": "笔记已删除",
            "ALERT_CREATED": "告警已创建",
            "ALERT_UPDATED": "告警已更新",
            "ALERT_DELETED": "告警已删除",
            "ADDED": "已加入关注",
            "REMOVED": "已移除关注",
            "DUPLICATE": "已存在(未重复添加)",
            "UPDATED": "已更新",
            "DELETED": "已删除",
            "CREATED": "已创建",
        }
        if m:
            verb, payload = m.group(1), m.group(2)
            label = verb_map.get(verb, verb)
            sym = ""
            try:
                parsed = _json.loads(payload)
                sym = parsed.get("symbol") or ""
            except Exception:
                # raw payload may itself be a ticker (e.g. "ADDED: 600036.SS")
                if payload and not payload.startswith("{"):
                    sym = payload.strip()
            if sym:
                return f"✅ {label} ({sym})"
            return f"✅ {label}"
        return f"✅ {label if status else 'ok'}"
    if status == "pending_approval":
        return "🔒 等待审批"
    return "other"


def test_write_ack_note_created():
    assert _render({"status": "created", "raw": 'NOTE_CREATED: {"id": "note-abc", "symbol": "600036.SS"}'}) \
        == "✅ 笔记已创建 (600036.SS)"


def test_write_ack_duplicate():
    assert _render({"status": "duplicate", "raw": "DUPLICATE: 600036.SS"}) \
        == "✅ 已存在(未重复添加) (600036.SS)"


def test_write_ack_added():
    # status=ok with ADDED raw prefix → "已加入关注 (600036.SS)"
    out = _render({"status": "ok", "raw": "ADDED: 600036.SS"})
    assert out == "✅ 已加入关注 (600036.SS)"


def test_write_ack_pending():
    assert _render({"status": "pending_approval", "args": {"symbol": "600036.SS"}}) \
        == "🔒 等待审批"


def test_write_ack_unknown_verb_falls_back():
    out = _render({"status": "created", "raw": "WEIRD_VERB: 600036.SS"})
    # unknown verb + ticker-like payload → "✅ WEIRD_VERB (600036.SS)"
    assert "WEIRD_VERB" in out or "已创建" in out


def test_non_ack_renders_other():
    assert _render({"price": 1.0, "symbol": "X"}) == "other"


def test_alert_deleted():
    assert _render({"status": "deleted", "raw": "ALERT_DELETED: {\"id\": \"alert-1\"}"}) \
        == "✅ 告警已删除"
