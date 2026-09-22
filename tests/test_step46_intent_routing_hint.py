"""§0.4.13 — Tool routing hint + cheat sheet (Plan B + D).

Two changes the user accepted:
- Plan B — every known Intent gets a one-line routing hint injected into
  the planner user prompt. UNKNOWN gets a recovery paragraph so the
  planner can still pick a tool when classification fell through.
- Plan D — append a "Tool Routing Cheat Sheet" block to the synthesizer
  system prompt so SynthesizeNode can cite tools by exact name.

These tests cover the wiring at the unit level (we don't gate on the LLM
itself; that's exercised in the e2e harness runs).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ----------------------------------------------------------------------
# Plan B — per-intent routing hint (planner)
# ----------------------------------------------------------------------
def test_routing_hint_module_covers_all_intent_values():
    from tradingagents.agent_harness.core.tier import Intent
    from tradingagents.agent_harness.core.tool_routing_hint import (
        _INTENT_ROUTING_HINTS, routing_hint,
    )
    # Every Intent enum value must have a hint (regression: a new Intent
    # added without updating tool_routing_hint.py would silently fall back
    # to the unknown paragraph — we'd miss the regression without this).
    for intent in Intent:
        assert intent in _INTENT_ROUTING_HINTS, (
            f"missing hint for Intent.{intent.name}"
        )
        h = routing_hint(intent)
        assert h, f"empty hint for Intent.{intent.name}"
        assert isinstance(h, str)


def test_routing_hint_accepts_str_and_intent():
    from tradingagents.agent_harness.core.tier import Intent
    from tradingagents.agent_harness.core.tool_routing_hint import routing_hint

    h_enum = routing_hint(Intent.HISTORY)
    h_str = routing_hint("history")
    assert h_enum == h_str, "str and Intent enum must produce identical hints"

    # Unknown string falls back to the UNKNOWN paragraph (defensive)
    h_bogus = routing_hint("not_a_real_intent")
    h_unknown = routing_hint(Intent.UNKNOWN)
    assert h_bogus == h_unknown


def test_routing_hint_history_mentions_get_history_not_get_quote():
    """Regression for the §0.4.12 incident: history intent must NOT
    recommend get_quote (the snapshot-only tool)."""
    from tradingagents.agent_harness.core.tier import Intent
    from tradingagents.agent_harness.core.tool_routing_hint import routing_hint

    h = routing_hint(Intent.HISTORY)
    assert "get_history" in h
    # The hint must explicitly warn against picking get_quote here
    assert "不要用 get_quote" in h or "不要用get_quote" in h


def test_build_plan_prompt_injects_routing_hint_for_known_intent():
    from dataclasses import dataclass, field
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    from tradingagents.agent_harness.core.tier import Intent

    @dataclass
    class _S:
        intent: object = Intent.QUOTE
        symbols: list = field(default_factory=list)
        carry_symbols: list = field(default_factory=list)
        user_message: str = "AAPL 现在多少"
        tool_results: list = field(default_factory=list)
        prior_user_msg: str | None = None

    class _AR:
        def list(self): return ["data_agent"]
        def get(self, n):
            class _A:
                description = "Fetch quote + fundamentals"
            return _A()

    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_registry = _AR()

    s = _S()
    s.intent = Intent.HISTORY
    s.user_message = "AAPL 最近 30 天走势"
    prompt = orch._build_plan_prompt(s)
    assert "Routing hint:" in prompt
    assert "本轮意图=history" in prompt
    assert "get_history" in prompt
    # Carry-forward NOTE block NOT injected (no carry_symbols)
    assert "Prior turn user message" not in prompt


def test_build_plan_prompt_includes_unknown_recovery_for_unknown_intent():
    from dataclasses import dataclass, field
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    from tradingagents.agent_harness.core.tier import Intent

    @dataclass
    class _S:
        intent: object = Intent.UNKNOWN
        symbols: list = field(default_factory=list)
        carry_symbols: list = field(default_factory=list)
        user_message: str = "嗯"
        tool_results: list = field(default_factory=list)
        prior_user_msg: str | None = None

    class _AR:
        def list(self): return []
        def get(self, n): return None

    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_registry = _AR()

    s = _S()
    s.intent = Intent.UNKNOWN
    prompt = orch._build_plan_prompt(s)
    assert "Routing hint:" in prompt
    assert "本轮意图=unknown" in prompt
    # Recovery paragraph should mention quote / history / fundamentals
    assert "quote" in prompt.lower()
    assert "history" in prompt.lower()


# ----------------------------------------------------------------------
# Plan D — Tool Routing Cheat Sheet (synthesizer)
# ----------------------------------------------------------------------
def test_cheat_sheet_is_non_empty():
    from tradingagents.agent_harness.core.tool_routing_hint import cheat_sheet
    cs = cheat_sheet()
    assert cs and isinstance(cs, str)
    # Section header
    assert "Cheat Sheet" in cs
    # Must enumerate the four routing categories
    assert "行情数据" in cs
    assert "CRUD" in cs
    assert "分析与报告" in cs


def test_cheat_sheet_covers_every_intent_class_tool():
    """Regression: a tool the LLM might need to cite must appear in the
    cheat sheet so SynthesizeNode doesn't have to guess the exact name."""
    from tradingagents.agent_harness.core.tool_routing_hint import cheat_sheet
    cs = cheat_sheet()
    expected_tools = [
        # data
        "get_quote", "get_quotes_batch", "get_history",
        "get_fundamentals", "get_news",
        "compute_alpha_factors", "list_alpha_factors",
        # crud
        "list_watchlist", "add_to_watchlist", "remove_from_watchlist",
        "list_notes", "create_note", "update_note", "delete_note",
        "list_alerts", "create_alert", "update_alert", "delete_alert",
        "list_scheduled_tasks", "create_scheduled_task",
        "update_scheduled_task", "delete_scheduled_task",
        "run_scheduled_task",
        # analysis
        "list_reports", "get_report",
        "list_runs", "get_analysis_status", "cancel_analysis_run",
        "run_trading_agents_analysis",
    ]
    missing = [t for t in expected_tools if t not in cs]
    assert not missing, f"cheat sheet missing tools: {missing}"


def test_synth_system_prompt_ends_with_cheat_sheet():
    """The synthesizer's system prompt must include the cheat sheet so
    SynthesizeNode anchors its citations to the exact tool names."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    sys_prompt = Orchestrator._SYNTH_SYSTEM
    assert "Tool Routing Cheat Sheet" in sys_prompt
    assert "§0.4.13" in sys_prompt
    # The cheat sheet itself should be at the tail of the system prompt
    # (after the citation contract block).
    cheat_idx = sys_prompt.find("Tool Routing Cheat Sheet")
    citation_idx = sys_prompt.find("Citation contract")
    assert citation_idx >= 0
    assert cheat_idx > citation_idx, (
        "cheat sheet must come AFTER the citation contract so the LLM "
        "reads grounding rules before the tool-name cheat sheet"
    )


# ----------------------------------------------------------------------
# Cross-cutting — the existing _PLAN_SYSTEM prompt is unchanged
# ----------------------------------------------------------------------
def test_plan_system_prompt_unchanged_after_plumbing():
    """Sanity: we touched _build_plan_prompt (the user-side prompt), not
    the system prompt. The §PLAN_SYSTEM constant must be byte-identical
    to what it was before this change."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    sys_prompt = Orchestrator._PLAN_SYSTEM
    # The agent description line that lists available agents
    assert "Available agents and their primary tools are listed" in sys_prompt
    # Should NOT contain the cheat sheet (that's synthesizer-only)
    assert "Tool Routing Cheat Sheet" not in sys_prompt
