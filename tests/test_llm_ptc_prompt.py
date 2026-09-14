"""LLM prompt引导 PTC — _parse_plan + _validate_ptc + _normalise_ptc_plan.

P1 priority: lets the LLM emit concurrent PTC programs instead of
sequential lists. Without this, every plan is serial — even when
multiple independent calls could run in parallel.
"""
from __future__ import annotations

import asyncio
import json

from tradingagents.agent_harness.core.orchestrator import Orchestrator
from tradingagents.agent_harness.core.tier import Intent


# --------------------------------------------------------------------------
# _parse_plan — accepts both list (legacy) and dict-with-mode-ptc shapes
# --------------------------------------------------------------------------
class TestParsePlan:
    def test_sequential_list_passes_through(self):
        text = json.dumps([
            {"step": 1, "agent": "data_agent", "args": {"symbol": "600036.SS"}},
            {"step": 2, "agent": "news_agent", "args": {"symbol": "600036.SS"}},
        ])
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(session_id="t", user_message="x")
        result = Orchestrator._parse_plan(text, state)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_ptc_dict_returns_ptc_program(self):
        text = json.dumps({
            "mode": "ptc",
            "groups": [{
                "id": "g1",
                "calls": [
                    {"agent": "data_agent", "args": {"symbol": "600036.SS"}},
                    {"agent": "data_agent", "args": {"symbol": "600418.SS"}},
                ],
            }],
        })
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(session_id="t", user_message="x")
        result = Orchestrator._parse_plan(text, state)
        assert isinstance(result, dict)
        assert result["mode"] == "ptc"
        assert len(result["groups"]) == 1

    def test_empty_plan_for_a_class(self):
        text = json.dumps([])
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(session_id="t", user_message="今天周几")
        result = Orchestrator._parse_plan(text, state)
        assert result == []

    def test_markdown_fence_stripped(self):
        text = "```json\n" + json.dumps([{"step": 1, "agent": "data_agent", "args": {}}]) + "\n```"
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(session_id="t", user_message="x")
        result = Orchestrator._parse_plan(text, state)
        assert isinstance(result, list)

    def test_malformed_json_returns_empty(self):
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(session_id="t", user_message="x")
        result = Orchestrator._parse_plan("not json", state)
        assert result == []

    def test_unexpected_shape_returns_empty(self):
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(session_id="t", user_message="x")
        result = Orchestrator._parse_plan(json.dumps({"foo": "bar"}), state)
        assert result == []


# --------------------------------------------------------------------------
# _validate_ptc — structural sanity check
# --------------------------------------------------------------------------
class TestValidatePtc:
    def test_valid_minimal_program(self):
        prog = {
            "mode": "ptc",
            "groups": [{
                "id": "g1",
                "calls": [{"name": "get_quote", "args": {"symbol": "X"}}],
            }],
        }
        assert Orchestrator._validate_ptc(prog) is True

    def test_valid_multi_group_with_dependencies(self):
        prog = {
            "mode": "ptc",
            "groups": [
                {"id": "g1", "calls": [{"name": "get_quote", "args": {"symbol": "X"}}]},
                {"id": "g2", "calls": [{"name": "get_news", "args": {"symbol": "X"}}],
                 "depends_on": ["g1"]},
            ],
        }
        assert Orchestrator._validate_ptc(prog) is True

    def test_rejects_wrong_mode(self):
        prog = {"mode": "sequential", "groups": []}
        assert Orchestrator._validate_ptc(prog) is False

    def test_rejects_empty_groups(self):
        prog = {"mode": "ptc", "groups": []}
        assert Orchestrator._validate_ptc(prog) is False

    def test_rejects_non_dict_program(self):
        assert Orchestrator._validate_ptc([]) is False
        assert Orchestrator._validate_ptc("ptc") is False

    def test_rejects_duplicate_group_ids(self):
        prog = {
            "mode": "ptc",
            "groups": [
                {"id": "g1", "calls": [{"name": "t", "args": {}}]},
                {"id": "g1", "calls": [{"name": "t", "args": {}}]},
            ],
        }
        assert Orchestrator._validate_ptc(prog) is False

    def test_rejects_group_with_empty_calls(self):
        prog = {
            "mode": "ptc",
            "groups": [{"id": "g1", "calls": []}],
        }
        assert Orchestrator._validate_ptc(prog) is False

    def test_rejects_call_without_name_or_agent(self):
        prog = {
            "mode": "ptc",
            "groups": [{"id": "g1", "calls": [{"args": {"symbol": "X"}}]}],
        }
        assert Orchestrator._validate_ptc(prog) is False

    def test_rejects_call_with_non_dict_args(self):
        prog = {
            "mode": "ptc",
            "groups": [{"id": "g1", "calls": [{"name": "t", "args": "not a dict"}]}],
        }
        assert Orchestrator._validate_ptc(prog) is False

    def test_accepts_depends_on_as_list_of_strings(self):
        prog = {
            "mode": "ptc",
            "groups": [
                {"id": "g1", "calls": [{"name": "t", "args": {}}]},
                {"id": "g2", "calls": [{"name": "t", "args": {}}], "depends_on": ["g1"]},
            ],
        }
        assert Orchestrator._validate_ptc(prog) is True

    def test_rejects_depends_on_as_non_list(self):
        prog = {
            "mode": "ptc",
            "groups": [
                {"id": "g1", "calls": [{"name": "t", "args": {}}]},
                {"id": "g2", "calls": [{"name": "t", "args": {}}], "depends_on": "g1"},
            ],
        }
        assert Orchestrator._validate_ptc(prog) is False


# --------------------------------------------------------------------------
# _normalise_ptc_plan — translate agent → name
# --------------------------------------------------------------------------
class TestNormalisePtcPlan:
    def test_agent_to_first_tool_mapping(self):
        plan = {
            "mode": "ptc",
            "groups": [{
                "id": "g1",
                "calls": [
                    {"agent": "data_agent", "args": {"symbol": "X"}},
                    {"agent": "news_agent", "args": {"symbol": "X"}},
                    {"agent": "alpha_agent", "args": {"symbol": "X"}},
                ],
            }],
        }
        out = Orchestrator._normalise_ptc_plan(plan)
        names = [c["name"] for c in out["groups"][0]["calls"]]
        assert names == ["get_quote", "get_news", "list_alpha_factors"]

    def test_passthrough_when_name_already_present(self):
        plan = {
            "mode": "ptc",
            "groups": [{
                "id": "g1",
                "calls": [{"name": "custom_tool", "args": {}}],
            }],
        }
        out = Orchestrator._normalise_ptc_plan(plan)
        assert out["groups"][0]["calls"][0]["name"] == "custom_tool"

    def test_unknown_agent_falls_back_to_agent_name(self):
        plan = {
            "mode": "ptc",
            "groups": [{
                "id": "g1",
                "calls": [{"agent": "totally_new_agent", "args": {}}],
            }],
        }
        out = Orchestrator._normalise_ptc_plan(plan)
        assert out["groups"][0]["calls"][0]["name"] == "totally_new_agent"

    def test_preserves_depends_on(self):
        plan = {
            "mode": "ptc",
            "groups": [
                {"id": "g1", "calls": [{"agent": "data_agent", "args": {}}]},
                {"id": "g2", "calls": [{"name": "t", "args": {}}], "depends_on": ["g1"]},
            ],
        }
        out = Orchestrator._normalise_ptc_plan(plan)
        assert out["groups"][1]["depends_on"] == ["g1"]

    def test_no_mutation_of_input(self):
        plan = {
            "mode": "ptc",
            "groups": [{"id": "g1", "calls": [{"agent": "data_agent", "args": {"symbol": "X"}}]}],
        }
        original = json.loads(json.dumps(plan))
        Orchestrator._normalise_ptc_plan(plan)
        assert plan == original, "should not mutate input"


# --------------------------------------------------------------------------
# End-to-end: _plan() returns PTC dict when LLM emits valid PTC
# --------------------------------------------------------------------------
class TestPlanEmitsPtc:
    def test_llm_emits_ptc_returns_ptc_dict(self):
        from unittest.mock import MagicMock as MM

        # Stub the LLM provider to return a PTC program
        class _StubProvider:
            def __init__(self):
                self.content = json.dumps({
                    "mode": "ptc",
                    "groups": [{
                        "id": "g1",
                        "calls": [
                            {"agent": "data_agent", "args": {"symbol": "600036.SS"}},
                            {"agent": "data_agent", "args": {"symbol": "600418.SS"}},
                        ],
                    }],
                })
            @property
            def name(self): return "stub"
            def complete(self, messages, **kwargs):
                from tradingagents.agent_harness.llm.base import LLMResponse
                return LLMResponse(content=self.content, provider="stub", model="m")
            def complete_text(self, prompt=None, **kwargs):
                from tradingagents.agent_harness.llm.base import LLMResponse
                return LLMResponse(content=self.content, provider="stub", model="m")

        class _StubFactory:
            def is_configured(self): return True
            def make(self): return _StubProvider()

        orch = Orchestrator(
            tool_registry=MM(list=lambda: [], get=lambda n: (_ for _ in ()).throw(KeyError(n))),
            agent_registry=MM(list_names=lambda: [], get=lambda n: MM(description="")),
            llm_factory=_StubFactory(),
            context_priority=MM(),
            retry_policy=MM(max_retries=0),
            circuit_breaker=MM(),
            audit=None,
            enable_l3=False,
        )
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        from tradingagents.agent_harness.tools import ToolContext

        state = OrchestratorState(
            session_id="t",
            user_message="比较 600036 和 600418",
            intent=Intent.COMPARE,
            symbols=["600036.SS", "600418.SS"],
        )
        plan = asyncio.run(orch._plan(state, ToolContext(session_id="t")))
        assert isinstance(plan, dict)
        assert plan["mode"] == "ptc"
        assert len(plan["groups"]) == 1
        names = sorted(c["name"] for c in plan["groups"][0]["calls"])
        assert names == ["get_quote", "get_quote"]

    def test_llm_emits_sequential_returns_list(self):
        class _StubProvider:
            content = json.dumps([
                {"step": 1, "agent": "data_agent", "args": {"symbol": "X"}},
            ])
            @property
            def name(self): return "stub"
            def complete(self, messages, **kwargs):
                from tradingagents.agent_harness.llm.base import LLMResponse
                return LLMResponse(content=self.content, provider="stub", model="m")
            def complete_text(self, prompt=None, **kwargs):
                from tradingagents.agent_harness.llm.base import LLMResponse
                return LLMResponse(content=self.content, provider="stub", model="m")

        class _StubFactory:
            def is_configured(self): return True
            def make(self): return _StubProvider()

        from unittest.mock import MagicMock as MM
        orch = Orchestrator(
            tool_registry=MM(list=lambda: [], get=lambda n: (_ for _ in ()).throw(KeyError(n))),
            agent_registry=MM(list_names=lambda: [], get=lambda n: MM(description="")),
            llm_factory=_StubFactory(),
            context_priority=MM(),
            retry_policy=MM(max_retries=0),
            circuit_breaker=MM(),
            audit=None,
            enable_l3=False,
        )
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        from tradingagents.agent_harness.tools import ToolContext

        state = OrchestratorState(
            session_id="t",
            user_message="查 600036",
            intent=Intent.QUOTE,
            symbols=["600036.SS"],
        )
        plan = asyncio.run(orch._plan(state, ToolContext(session_id="t")))
        assert isinstance(plan, list)
        assert plan[0]["agent"] == "data_agent"

    def test_llm_emits_malformed_ptc_falls_back_to_heuristic(self):
        """Invalid PTC → empty parse → heuristic kicks in."""
        class _StubProvider:
            content = json.dumps({"mode": "ptc", "groups": []})  # empty groups
            @property
            def name(self): return "stub"
            def complete(self, messages, **kwargs):
                from tradingagents.agent_harness.llm.base import LLMResponse
                return LLMResponse(content=self.content, provider="stub", model="m")
            def complete_text(self, prompt=None, **kwargs):
                from tradingagents.agent_harness.llm.base import LLMResponse
                return LLMResponse(content=self.content, provider="stub", model="m")

        class _StubFactory:
            def is_configured(self): return True
            def make(self): return _StubProvider()

        from unittest.mock import MagicMock as MM
        orch = Orchestrator(
            tool_registry=MM(list=lambda: [], get=lambda n: (_ for _ in ()).throw(KeyError(n))),
            agent_registry=MM(list_names=lambda: [], get=lambda n: MM(description="")),
            llm_factory=_StubFactory(),
            context_priority=MM(),
            retry_policy=MM(max_retries=0),
            circuit_breaker=MM(),
            audit=None,
            enable_l3=False,
        )
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        from tradingagents.agent_harness.tools import ToolContext

        state = OrchestratorState(
            session_id="t",
            user_message="比较 600036 600418",
            intent=Intent.COMPARE,
            symbols=["600036.SS", "600418.SS"],
        )
        plan = asyncio.run(orch._plan(state, ToolContext(session_id="t")))
        # Malformed PTC → empty parse → heuristic produces 2-symbol PTC
        assert isinstance(plan, dict)
        assert plan["mode"] == "ptc"
