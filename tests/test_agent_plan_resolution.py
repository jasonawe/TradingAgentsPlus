"""N122 fix: orchestrator accepts LLM-generated plans that use 'agent' field
in addition to canonical 'action' field. Resolves to first tool of agent.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core.orchestrator import Orchestrator  # noqa: E402


def test_resolve_action_with_action_field() -> None:
    """Canonical plan step: action=<tool_name>."""
    name, agent = Orchestrator._resolve_action({"action": "get_quote", "args": {"symbol": "X"}})
    assert name == "get_quote"
    assert agent == ""


def test_resolve_action_with_agent_field() -> None:
    """LLM-generated plan: agent=<sub_agent_name>."""
    name, agent = Orchestrator._resolve_action({"agent": "data_agent", "args": {"symbol": "600036.SS"}})
    assert name == "get_quote"
    assert agent == "data_agent"


def test_resolve_action_alpha_agent_maps_to_list_alpha_factors() -> None:
    name, agent = Orchestrator._resolve_action({"agent": "alpha_agent", "args": {}})
    assert name == "list_alpha_factors"
    assert agent == "alpha_agent"


def test_resolve_action_news_agent_maps_to_get_news() -> None:
    name, agent = Orchestrator._resolve_action({"agent": "news_agent", "args": {"symbol": "X"}})
    assert name == "get_news"
    assert agent == "news_agent"


def test_resolve_action_synthesizer_has_no_tool() -> None:
    """Pure-LLM agents have no tool → action="", agent stays as-is."""
    name, agent = Orchestrator._resolve_action({"agent": "synthesizer", "args": {}})
    assert name == ""
    assert agent == "synthesizer"


def test_resolve_action_unknown_agent_passes_through() -> None:
    """Unknown agent name → action="", agent=<unknown>; executor returns error."""
    name, agent = Orchestrator._resolve_action({"agent": "nonexistent_agent", "args": {}})
    assert name == ""
    assert agent == "nonexistent_agent"


def test_resolve_action_action_field_takes_priority() -> None:
    """If both action AND agent are set, action wins."""
    name, agent = Orchestrator._resolve_action({
        "action": "get_quote", "agent": "data_agent", "args": {}
    })
    assert name == "get_quote"
    assert agent == "data_agent"


def test_resolve_action_empty_step() -> None:
    name, agent = Orchestrator._resolve_action({})
    assert name == ""
    assert agent == ""
