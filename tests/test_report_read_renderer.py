"""§Step 27 — get_report friendly rendering.

The harness's get_report tool returns a single ``text`` string shaped
like ``REPORT: <id>\n---meta---\n<json>\n---content---\n<markdown>``.
Without a dedicated renderer this blob falls through to
``display_view_for``'s JSON-dump fallback and the chat bubble shows
the user a wall of escaped JSON.

These tests lock in:
1. The meta block is parsed as JSON and rendered as a 2-column
   table with friendly Chinese labels for the common fields.
2. The report content (markdown body) is preserved verbatim.
3. Nested dict / list meta fields (e.g. ``analysts: {...}``) are
   SKIPPED — their content already lives in the report body and
   re-emitting it as JSON just clutters the meta table.
4. The renderer does not crash on malformed input (no REPORT
   prefix, invalid JSON meta, plain markdown with no separators).
"""
from __future__ import annotations

from tradingagents.agent_harness.tools.display_view import (
    _render_report_read,
    display_view_for,
)


REPORT_ID = "run-55464f38279f4b36a8abc775a3a8f108"


def _full_payload() -> dict:
    return {
        "status": "ok",
        "text": (
            f"REPORT: {REPORT_ID}\n"
            f"---meta---\n"
            f'{{"report_id": "{REPORT_ID}", "run_id": "{REPORT_ID}", '
            f'"source": "web", "ticker": "600036.SS", '
            f'"analysis_date": "2026-09-02", '
            f'"generated_at": "2026-09-02T04:49:19.369975+00:00", '
            f'"signal": "Overweight", "rating": "Overweight", '
            f'"status": "completed", "asset_type": "stock", '
            f'"analysts": {{"market": "long analyst text", '
            f'"news": "long news summary text"}}, '
            f'"internal_id": 42}}\n'
            f"---content---\n"
            f"# 招商银行分析\n\n"
            f"## 核心观点\n\n"
            f"维持 **Overweight** 评级。\n"
        ),
    }


def test_render_report_read_extracts_id_and_meta_table():
    out = _render_report_read(_full_payload())
    assert out.startswith(f"## 📑 报告 {REPORT_ID}"), out[:200]
    assert "### 元数据" in out
    # Friendly Chinese labels.
    assert "| 标的 | `600036.SS` |" in out
    assert "| 信号 | `Overweight` |" in out
    assert "| 评级 | `Overweight` |" in out
    assert "| 状态 | `completed` |" in out
    # Raw report content survives intact.
    assert "# 招商银行分析" in out
    assert "维持 **Overweight** 评级" in out


def test_render_report_read_skips_nested_dict_meta_fields():
    """``analysts`` is a dict-of-strings payload — its content
    already lives in the report body. Re-emitting it as JSON just
    clutters the meta table."""
    out = _render_report_read(_full_payload())
    # The dict must NOT appear as a JSON blob in the meta table.
    assert '"market":' not in out
    assert '"news":' not in out
    # But scalar meta fields DO appear.
    assert "| 报告 ID |" in out
    # Internal numeric fields also appear (only nested dicts/list
    # are filtered).
    assert "| internal_id |" in out


def test_render_report_read_handles_missing_separators():
    """Legacy / hand-crafted inputs without ``---meta---`` /
    ``---content---`` markers should still render the body."""
    raw = (
        f"REPORT: {REPORT_ID}\n"
        f"# plain markdown report\n"
        f"with body text but no meta separator\n"
    )
    out = _render_report_read({"status": "ok", "text": raw})
    assert "## 📑 报告" in out
    assert "# plain markdown report" in out
    # No meta section when there's no marker.
    assert "### 元数据" not in out


def test_render_report_read_handles_invalid_json_meta():
    """If the meta block isn't valid JSON we fall back to a code
    fence rather than crashing the chat bubble."""
    raw = (
        f"REPORT: {REPORT_ID}\n"
        f"---meta---\n"
        f"this is not json\n"
        f"---content---\n"
        f"# body\n"
    )
    out = _render_report_read({"status": "ok", "text": raw})
    assert "this is not json" in out
    assert "```" in out


def test_display_view_for_dispatches_report_read():
    """End-to-end: ``display_view_for(result, intent='report_read')``
    must NOT fall back to JSON dump — it must return the markdown
    card (the bug we're fixing)."""
    out = display_view_for(_full_payload(), intent="report_read")
    assert isinstance(out, str)
    assert "## 📑 报告" in out
    # Crucial: the bug was the JSON dump fallback — assert that
    # meta is rendered as a markdown table, not as raw escaped JSON.
    assert '"ticker":' not in out
    assert '\\\\n' not in out


def test_render_report_read_handles_empty_input():
    """No text at all → friendly placeholder, never a crash."""
    assert _render_report_read({"status": "ok", "text": ""}) == "(空)"
    assert _render_report_read({}) == "(空)"
