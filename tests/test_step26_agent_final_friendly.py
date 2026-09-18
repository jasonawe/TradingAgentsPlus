"""Step 26 — agent_final bubble emits friendly summary.

The frontend's renderMarkdown() reads ``agent_final.result.summary``.
Step 22 P0 made write acks friendly on the JS side, but Tier 1 short-
circuit / Tier 2 synthesize was emitting raw dicts that the frontend
either JSON-dumped or silently rendered as one long line.

This test pins the backend contract: ``orchestrator._friendly_summary``
shapes a raw tool result into a markdown string using the tool's
``metadata["display_view"]`` hint + the capability enum, so the
frontend just runs renderMarkdown() and never sees raw JSON.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_friendly_summary_for_quote():
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    raw = {
        "symbol": "600036.SS",
        "price": 41.78,
        "change": 0.43,
        "change_pct": 1.04,
        "volume": 18187747,
    }
    out = Orchestrator._friendly_summary(raw, tool_name="get_quote")
    assert "600036.SS" in out
    assert "41.78" in out
    assert "+0.43" in out or "0.43" in out


def test_friendly_summary_for_history():
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    raw = {
        "symbol": "600036.SS",
        "interval": "1d",
        "candles": [{"close": 41.78, "timestamp": "2026-09-18"}],
    }
    out = Orchestrator._friendly_summary(raw, tool_name="get_history")
    assert "600036.SS" in out
    assert "1d" in out


def test_friendly_summary_for_news():
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    raw = {
        "symbol": "NVDA",
        "items": [
            {"title": "Earnings beat"},
            {"title": "AI demand"},
        ],
    }
    out = Orchestrator._friendly_summary(raw, tool_name="get_news")
    assert "NVDA" in out
    assert "Earnings beat" in out


def test_friendly_summary_for_write_ack():
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    raw = {"status": "created", "raw": "NOTE_CREATED: {\"id\": \"n1\", \"symbol\": \"600036.SS\"}"}
    out = Orchestrator._friendly_summary(raw, tool_name="create_note")
    assert "笔记已创建" in out or "已创建" in out
    assert "600036.SS" in out


def test_friendly_summary_for_list():
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    raw = {"count": 5, "text": "| symbol | price |\n| 600036 | 41.78 |"}
    out = Orchestrator._friendly_summary(raw, tool_name="list_notes")
    assert "5" in out
    assert "600036" in out


def test_friendly_summary_unknown_tool_falls_back_to_json():
    """Unknown tool_name → raw JSON dump (last-resort)."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    raw = {"foo": "bar"}
    out = Orchestrator._friendly_summary(raw, tool_name="some_unknown_tool")
    assert "foo" in out and "bar" in out


def test_friendly_summary_uses_metadata_when_tool_unnamed():
    """When tool_name is unknown but we pass a known intent, the
    helper still picks the right renderer."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    raw = {"symbol": "600036.SS", "price": 41.78}
    out = Orchestrator._friendly_summary(raw, intent="quote")
    assert "600036.SS" in out
