"""W3-D4 E7: AgentScope — per-agent tool restriction + LLM override + variables."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core.agent_scope import AgentScope


ALL_TOOLS = ["get_quote", "get_news", "create_alert", "delete_alert", "list_alpha_factors"]


# ---------------------------------------------------------------------------
# Tool restriction
# ---------------------------------------------------------------------------


def test_no_restriction_inherits_all() -> None:
    scope = AgentScope(name="root")
    assert scope.allowed_tools(ALL_TOOLS) == ALL_TOOLS


def test_explicit_whitelist_restricts() -> None:
    scope = AgentScope(name="read-only", tools=["get_quote", "get_news"])
    assert scope.allowed_tools(ALL_TOOLS) == ["get_quote", "get_news"]


def test_empty_whitelist_blocks_all() -> None:
    scope = AgentScope(name="locked-down", tools=[])
    assert scope.allowed_tools(ALL_TOOLS) == []


def test_whitelist_drops_unknown_tool_names() -> None:
    """Scope can only allow tools that exist in the harness registry.
    Stale entries are silently dropped (no surprise for the agent).
    """
    scope = AgentScope(name="x", tools=["get_quote", "nonexistent_tool"])
    assert scope.allowed_tools(ALL_TOOLS) == ["get_quote"]


def test_inherit_from_parent_when_none() -> None:
    parent = AgentScope(name="parent", tools=["get_quote", "get_news"])
    child = AgentScope(name="child", parent=parent)
    # child.tools is None → walks to parent
    assert child.allowed_tools(ALL_TOOLS) == ["get_quote", "get_news"]


def test_child_override_parent() -> None:
    parent = AgentScope(name="parent", tools=["get_quote", "get_news", "create_alert"])
    child = AgentScope(name="child", tools=["get_quote"], parent=parent)
    # child.tools is non-None → wins
    assert child.allowed_tools(ALL_TOOLS) == ["get_quote"]


def test_is_tool_allowed() -> None:
    scope = AgentScope(name="r", tools=["get_quote"])
    assert scope.is_tool_allowed("get_quote", ALL_TOOLS) is True
    assert scope.is_tool_allowed("create_alert", ALL_TOOLS) is False


def test_restrict_tools_mutates_in_place() -> None:
    scope = AgentScope(name="x")
    scope.restrict_tools(["get_quote"])
    assert scope.allowed_tools(ALL_TOOLS) == ["get_quote"]


# ---------------------------------------------------------------------------
# LLM overrides
# ---------------------------------------------------------------------------


def test_no_llm_override_returns_empty() -> None:
    scope = AgentScope(name="root")
    assert scope.llm_overrides() == {"provider": None, "model": None}


def test_llm_override_provider_only() -> None:
    scope = AgentScope(name="x", llm_provider="openai")
    assert scope.llm_overrides() == {"provider": "openai", "model": None}


def test_llm_override_model_only() -> None:
    scope = AgentScope(name="x", llm_model="gpt-4o")
    assert scope.llm_overrides() == {"provider": None, "model": "gpt-4o"}


def test_llm_override_inherited_from_parent() -> None:
    parent = AgentScope(name="parent", llm_provider="anthropic", llm_model="claude-opus")
    child = AgentScope(name="child", parent=parent)
    assert child.llm_overrides() == {"provider": "anthropic", "model": "claude-opus"}


def test_child_llm_override_wins() -> None:
    parent = AgentScope(name="p", llm_provider="anthropic")
    child = AgentScope(name="c", llm_provider="openai", parent=parent)
    assert child.llm_overrides() == {"provider": "openai", "model": None}


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------


def test_get_variable_own_wins() -> None:
    scope = AgentScope(name="x", variables={"tier": "pro"})
    assert scope.get_variable("tier") == "pro"


def test_get_variable_inherits_from_parent() -> None:
    parent = AgentScope(name="p", variables={"user": "alice"})
    child = AgentScope(name="c", parent=parent)
    assert child.get_variable("user") == "alice"


def test_get_variable_missing_returns_default() -> None:
    scope = AgentScope(name="x")
    assert scope.get_variable("missing") is None
    assert scope.get_variable("missing", "fallback") == "fallback"


def test_set_variable_mutates_in_place() -> None:
    scope = AgentScope(name="x")
    scope.set_variable("k", "v")
    assert scope.get_variable("k") == "v"


def test_merged_variables_child_wins() -> None:
    parent = AgentScope(name="p", variables={"a": 1, "b": 2})
    child = AgentScope(name="c", variables={"b": 99, "c": 3}, parent=parent)
    merged = child.merged_variables()
    assert merged == {"a": 1, "b": 99, "c": 3}


# ---------------------------------------------------------------------------
# Shadow
# ---------------------------------------------------------------------------


def test_shadow_returns_new_independent_scope() -> None:
    parent = AgentScope(name="p", tools=["get_quote"], variables={"k": "v"})
    child = AgentScope(name="c", llm_provider="openai")
    merged = child.shadow(parent)
    assert isinstance(merged, AgentScope)
    assert merged.parent is parent
    # Mutating merged doesn't affect either input
    merged.set_variable("extra", "x")
    assert "extra" not in child.variables
    assert "extra" not in parent.variables


def test_shadow_field_resolution_walks_chain() -> None:
    """shadow() chains the parent — child field lookups walk both."""
    parent = AgentScope(name="p", llm_provider="anthropic", variables={"u": "alice"})
    child = AgentScope(name="c", tools=["get_quote"])
    merged = child.shadow(parent)
    # tools comes from child (own)
    assert merged.allowed_tools(ALL_TOOLS) == ["get_quote"]
    # llm comes from parent (chain)
    assert merged.llm_overrides() == {"provider": "anthropic", "model": None}
    # variable comes from parent (chain)
    assert merged.get_variable("u") == "alice"


# ---------------------------------------------------------------------------
# Harness integration
# ---------------------------------------------------------------------------


def test_harness_default_scope_inherits_all_tools(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_DATA_DIR", str(tmp_path / "data"))
    from tradingagents.agent_harness.harness import Harness
    h = Harness()
    assert isinstance(h.default_agent_scope, AgentScope)
    all_tool_names = [t.name for t in h.tool_registry.list_all()]
    # Default scope = no restriction → all tools visible.
    assert h.default_agent_scope.allowed_tools(all_tool_names) == all_tool_names


def test_harness_wires_scope_into_agents(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_DATA_DIR", str(tmp_path / "data"))
    from tradingagents.agent_harness.harness import Harness
    h = Harness()
    planner = h.agent_registry.get("planner")
    assert planner.scope is h.default_agent_scope


def test_agent_can_use_independent_scope(tmp_path, monkeypatch) -> None:
    """An agent instantiated outside the harness can take its own scope."""
    monkeypatch.setenv("TRADINGAGENTS_DATA_DIR", str(tmp_path / "data"))
    from tradingagents.agent_harness.harness import Harness
    from tradingagents.agent_harness.agents import PlannerAgent
    h = Harness()
    read_only = AgentScope(name="read-only", tools=["get_quote"])
    # Build a fresh PlannerAgent with a restricted scope (no LLM needed).
    agent = PlannerAgent(llm_factory=None, tool_registry=None, scope=read_only)
    assert agent.scope.allowed_tools(ALL_TOOLS) == ["get_quote"]
    # Default harness scope is unchanged.
    assert h.default_agent_scope.allowed_tools(ALL_TOOLS) == ALL_TOOLS


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_repr_includes_key_fields() -> None:
    scope = AgentScope(
        name="x", tools=["a", "b"], llm_provider="p", variables={"k": "v"},
    )
    r = repr(scope)
    assert "x" in r and "2" in r  # 2 tools
