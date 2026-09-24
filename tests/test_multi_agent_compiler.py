import pytest
from tradingagents.agent_harness.runtime.multi_agent.compiler import (
    PlanCompiler, CompileError,
)
from tradingagents.agent_harness.runtime.multi_agent.graph import (
    GraphSpec, NodeKind, Edge,
)

class FakeCall:
    def __init__(self, tool, args, parallel_group=0):
        self.tool = tool
        self.args = args
        self.parallel_group = parallel_group

class FakePlan:
    def __init__(self, calls):
        self.calls = calls

def test_compiler_single_group_emits_one_tool_node_per_call_and_join():
    plan = FakePlan([
        FakeCall("get_quote", {"symbol": "600036.SS"}, parallel_group=0),
        FakeCall("get_fundamentals", {"symbol": "600036.SS"}, parallel_group=0),
    ])
    spec = PlanCompiler().compile(plan)
    tool_nodes = [n for n in spec.nodes.values() if n.kind == NodeKind.TOOL]
    assert len(tool_nodes) == 2
    assert {n.id for n in tool_nodes} == {"call_0", "call_1"}
    edges = {(e.src, e.dst) for e in spec.edges}
    assert ("call_0", "join_0") in edges
    assert ("call_1", "join_0") in edges

def test_compiler_two_groups_get_cross_group_data_edge_from_group_order():
    plan = FakePlan([
        FakeCall("get_quote", {"symbol": "x"}, parallel_group=0),
        FakeCall("compute_alpha_factors", {"symbol": "x"}, parallel_group=1),
    ])
    spec = PlanCompiler().compile(plan)
    edges = {(e.src, e.dst) for e in spec.edges}
    assert ("join_0", "call_1") in edges

def test_compiler_three_groups_chain_through_joins():
    plan = FakePlan([
        FakeCall("get_quote", {}, parallel_group=0),
        FakeCall("get_fundamentals", {}, parallel_group=1),
        FakeCall("compute_alpha_factors", {}, parallel_group=2),
    ])
    spec = PlanCompiler().compile(plan)
    edges = {(e.src, e.dst) for e in spec.edges}
    assert ("join_0", "call_1") in edges
    assert ("join_1", "call_2") in edges

def test_compiler_maps_tool_to_agent_via_TOOL_TO_AGENT():
    plan = FakePlan([
        FakeCall("get_quote", {}, parallel_group=0),
        FakeCall("add_to_watchlist", {}, parallel_group=0),
        FakeCall("run_trading_agents_analysis", {}, parallel_group=0),
    ])
    spec = PlanCompiler().compile(plan)
    by_id = {n.id: n for n in spec.nodes.values()}
    assert by_id["call_0"].agent_id == "data_agent"
    assert by_id["call_1"].agent_id == "command_resolver"
    assert by_id["call_2"].agent_id == "trading_agents"

def test_compiler_rejects_unknown_tool():
    plan = FakePlan([FakeCall("definitely_not_a_tool", {}, parallel_group=0)])
    with pytest.raises(CompileError):
        PlanCompiler().compile(plan)

def test_compiler_rejects_empty_plan():
    with pytest.raises(CompileError):
        PlanCompiler().compile(FakePlan([]))
