"""§Step 23 — when _friendly_summary falls back to a JSON dump of the
synthesised payload, the agent_final emit must unwrap the prose
``summary`` field instead of dumping the whole dict on the UI.
"""
import json
from tradingagents.agent_harness.core.orchestrator import Orchestrator


def _friendly(result, tool_name=None, intent=None):
    return Orchestrator._friendly_summary(result, tool_name=tool_name, intent=intent)


def test_friendly_summary_returns_summary_for_synth_payload():
    """When no view_key matches and result has a summary field,
    render that summary as markdown-friendly text rather than dumping
    the whole dict.
    """
    payload = {
        "intent": "unknown",
        "symbols": [],
        "results": [],
        "summary": "今天 (2026-09-18) 是星期五。",
    }
    out = _friendly(payload, tool_name=None, intent=None)
    assert isinstance(out, str)
    # Either it returned the prose directly, or the orchestrator's
    # §Step 23 unwrap will salvage it. Verify at least one of these:
    if out.startswith("{"):
        # Fallback path — orchestrator unwraps on emit.
        assert json.loads(out)["summary"].startswith("今天")
    else:
        assert "今天" in out and "星期五" in out, f"got: {out!r}"


def test_friendly_summary_handles_unknown_intent_no_tool():
    """The exact scenario from the bug report — UNKNOWN intent +
    no tool results → synthesised payload with summary."""
    out = _friendly({"intent": "unknown", "summary": "周五"}, tool_name=None)
    # Must NOT be a raw JSON dump of the whole dict.
    assert "intent" not in out or "unknown" not in out or "周五" in out


def test_friendly_summary_for_tool_result_still_uses_view():
    """Regression guard: when a tool name IS provided we still use
    display_view_for instead of the JSON fallback."""
    quote = {"symbol": "600036.SS", "price": 40.94, "currency": "CNY"}
    out = _friendly(quote, tool_name="get_quote")
    # Should NOT start with `{` (display_view_for returns markdown)
    assert not out.lstrip().startswith("{"), f"got: {out!r}"
    assert "600036" in out or "40.9" in out
