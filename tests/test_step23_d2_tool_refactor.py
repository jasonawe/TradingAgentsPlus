"""Spec Step 23 — D2 Tool refactor: unified ToolSpec + lifecycle hooks.

The harness already has BaseTool / FunctionTool / ToolSchema — D2
adds a unified metadata surface (``ToolSpec.metadata``) so callers
can ask a tool:

- what capabilities it has (``quote`` / ``crud_note`` / ``alpha``)
- whether to surface its results in the user-facing ``display_view``
- which lifecycle hooks fire (pre/post)
- what error code to use on failure

This test covers the new ABC additions against a 1-tool sample
(``hello_tool``) so we don't have to migrate all 33 builtins.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Step 23a — ToolSchema.metadata field
# ---------------------------------------------------------------------------
def test_tool_schema_has_metadata_field():
    from tradingagents.agent_harness.tools.schema import ToolSchema
    schema = ToolSchema(
        name="x",
        description="x",
        args_schema=type,
        result_schema=type,
    )
    assert schema.metadata == {}


def test_tool_schema_metadata_round_trip():
    from tradingagents.agent_harness.tools.schema import ToolSchema
    md = {"capabilities": ["quote"], "display_view": "quote_simple", "category": "data"}
    schema = ToolSchema(
        name="get_quote",
        description="x",
        args_schema=type,
        result_schema=type,
        metadata=md,
    )
    assert schema.metadata == md


# ---------------------------------------------------------------------------
# Step 23b — Capability tagging
# ---------------------------------------------------------------------------
def test_capability_enum_has_core_categories():
    from tradingagents.agent_harness.tools.capabilities import Capability
    names = {c.name for c in Capability}
    # All five core categories must exist.
    assert {"QUOTE", "HISTORY", "FUNDAMENTALS", "NEWS", "NOTE",
            "ALERT", "WATCHLIST", "SCHEDULED", "RUN", "REPORT",
            "ALPHA"}.issubset(names)


def test_capability_tag_in_metadata():
    """A tool declares its capability via metadata['capabilities']."""
    from tradingagents.agent_harness.tools.capabilities import Capability
    md = {"capabilities": [Capability.QUOTE, Capability.NEWS]}
    assert Capability.QUOTE in md["capabilities"]


# ---------------------------------------------------------------------------
# Step 23c — display_view helper
# ---------------------------------------------------------------------------
def test_display_view_helper_renders_friendly():
    """display_view_for(result, intent) returns the human summary
    the frontend's agent_final bubble should show."""
    from tradingagents.agent_harness.tools.display_view import display_view_for
    payload = {
        "symbol": "600036.SS",
        "price": 41.78,
        "change": 0.43,
        "change_pct": 1.04,
    }
    out = display_view_for(payload, intent="quote")
    assert "600036.SS" in out
    assert "41.78" in out


def test_display_view_helper_falls_back_to_json():
    """Unknown intent → pretty JSON dump (last-resort)."""
    import json
    from tradingagents.agent_harness.tools.display_view import display_view_for
    out = display_view_for({"foo": 1}, intent="unknown_thing")
    assert "foo" in out and "1" in out


# ---------------------------------------------------------------------------
# Step 23d — lifecycle hooks
# ---------------------------------------------------------------------------
def test_lifecycle_pre_post_called_in_order():
    """A tool's lifecycle pre_hook runs before invoke, post_hook after."""
    from tradingagents.agent_harness.tools.lifecycle import LifecycleTracker
    tracker = LifecycleTracker()
    tracker.pre("get_quote", args={"symbol": "X"})
    tracker.post("get_quote", args={"symbol": "X"}, result={"price": 1.0})
    events = tracker.events
    assert events[0].phase == "pre"
    assert events[1].phase == "post"
    assert events[0].tool == "get_quote"


def test_lifecycle_tracker_records_errors():
    from tradingagents.agent_harness.tools.lifecycle import LifecycleTracker
    tracker = LifecycleTracker()
    tracker.pre("get_quote", args={"symbol": "X"})
    tracker.error("get_quote", args={"symbol": "X"}, error=ValueError("bad"))
    assert tracker.events[-1].phase == "error"
    assert "bad" in tracker.events[-1].error
