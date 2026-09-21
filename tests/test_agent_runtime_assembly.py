"""Tasks 19-21 — Harness assembly + TurnCoordinator + Workflow migration.

覆盖 plan 要求:
- Task 19: Harness 装配 RuntimeStore / executors / agents / policy / scheduler /
  dispatcher / outbox / runtime
- Task 20: TurnCoordinator Tier 1 直读 zero-LLM,Tier 2/3 创建 run
- Task 21: Workflow services 迁移(workflow_spec_registry 入口)
"""
from __future__ import annotations

import pytest


# ════════════════════════════════════════════════════════
# Task 19 — Harness assembly
# ════════════════════════════════════════════════════════


def test_harness_exposes_runtime_components(tmp_path):
    """Harness 装配后暴露 runtime_store / scheduler / dispatcher / runtime / policy。"""
    from tradingagents.agent_harness.harness import Harness
    h = Harness.from_data_dir(tmp_path)
    assert hasattr(h, "runtime_store")
    assert hasattr(h, "scheduler")
    assert hasattr(h, "dispatcher")
    assert hasattr(h, "runtime")
    assert hasattr(h, "policy")
    assert hasattr(h, "trace_projector")


def test_harness_loads_six_v2_agents(tmp_path):
    """Harness 装载 6 个 V2 agents(planner / data / news / alpha / verifier / synthesizer)。"""
    from tradingagents.agent_harness.harness import Harness
    h = Harness.from_data_dir(tmp_path)
    for name in ("planner", "data_agent", "news_agent", "alpha_agent",
                 "verifier", "synthesizer"):
        assert h.agent_registry.has(name) or h.agent_registry.descriptor(name) is not None


def test_harness_runtime_db_path(tmp_path):
    """RuntimeStore DB 在 {data_dir}/agent_runtime.sqlite。"""
    from tradingagents.agent_harness.harness import Harness
    h = Harness.from_data_dir(tmp_path)
    expected = tmp_path / "agent_runtime.sqlite"
    assert str(h.runtime_store.db_path) == str(expected.resolve())


# ════════════════════════════════════════════════════════
# Task 20 — TurnCoordinator
# ════════════════════════════════════════════════════════


def test_turn_coordinator_tier1_no_llm(tmp_path):
    """Tier 1 直读 → ToolExecutor + ResultFormatter,zero LLM 调用。"""
    from tradingagents.agent_harness.core.turn_coordinator import TurnCoordinator
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op

    s = AgentRuntimeStore(tmp_path / "runtime.sqlite")
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["AAPL"], carry_symbols=[], slots={},
        tier=1, confidence=1.0,
        reason_code="t", route_kind="DIRECT_READ",
    )
    class _StubTool:
        async def invoke(self, tool_name, args):
            return {"symbol": "AAPL", "price": 100.0}

    class _StubFmt:
        def format(self, *, tier, content, route):
            return {"tier": tier, "content": content, "route_kind": route.route_kind}

    tc = TurnCoordinator(
        store=s,
        tool_executor=_StubTool(),
        result_formatter=_StubFmt(),
        llm_calls_counter={"n": 0},
    )
    result = tc.handle(route=route, user_message="get AAPL quote", session_id="s1")
    assert result["tier"] == 1
    assert tc.llm_calls_counter["n"] == 0  # zero LLM


def test_turn_coordinator_tier3_creates_agent_run(tmp_path):
    """Tier 3 分析 → AGENT_ANALYSIS run。"""
    from tradingagents.agent_harness.core.turn_coordinator import TurnCoordinator
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op

    s = AgentRuntimeStore(tmp_path / "runtime.sqlite")
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["AAPL"], carry_symbols=[], slots={},
        tier=3, confidence=1.0,
        reason_code="t", route_kind="AGENT_ANALYSIS",
    )
    class _StubTool:
        async def invoke(self, tool_name, args):
            return {}
    class _StubFmt:
        def format(self, **kw):
            return kw
    tc = TurnCoordinator(
        store=s,
        tool_executor=_StubTool(),
        result_formatter=_StubFmt(),
        llm_calls_counter={"n": 0},
    )
    run = tc.start_agent_run(
        session_id="s1", turn_id="t1", route=route,
        planner_agent="planner", planner_capability="planning",
    )
    assert run["run_kind"] == "AGENT_ANALYSIS"


# ════════════════════════════════════════════════════════
# Task 21 — Workflow spec registry
# ════════════════════════════════════════════════════════


def test_workflow_spec_registry_round_trip():
    """register + get 一个 workflow spec。"""
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry, WorkflowSpec,
    )
    reg = WorkflowSpecRegistry()
    spec = WorkflowSpec(
        name="test_workflow",
        steps=[{"step": 1, "agent": "data_agent", "args": {}}],
        required_agents=["data_agent"],
        timeout_seconds=60.0,
    )
    reg.register(spec)
    got = reg.get("test_workflow")
    assert got.name == "test_workflow"
    assert "data_agent" in got.required_agents
