"""Tests for runtime/multi_agent/compiler.py (Phase 3).

Per plan Work unit 2 (spec §4.5 / §4.6 step 2 / step 4):
  - Same-group + $ref → no extra edge (group ordering handles it)
  - Adjacent-upstream + $ref → data edge (forward dep)
  - Skip-upstream + $ref → loop edge with max_hops=2 (feedback)
  - $ref to unknown agent (not in plan) → CompileError (per-agent scoping)
  - $ref to own agent (self-ref) → CompileError
  - $ref to downstream group → CompileError (per-agent scoping)
  - Multiple $refs → multiple edges
  - Phase 1/2 regression: 6 existing tests still pass
"""
import pytest

from tradingagents.agent_harness.runtime.multi_agent.compiler import (
    CompileError,
    PlanCompiler,
)
from tradingagents.agent_harness.runtime.multi_agent.graph import (
    Edge,
    GraphSpec,
    NodeKind,
)


class FakeCall:
    def __init__(self, tool, args, parallel_group=0):
        self.tool = tool
        self.args = args
        self.parallel_group = parallel_group


class FakePlan:
    def __init__(self, calls):
        self.calls = calls


# ────────────────────────────────────────────────────────────────────────
# Phase 1 + 2 regression tests (preserved verbatim, docstring updated)
# ────────────────────────────────────────────────────────────────────────

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


# ────────────────────────────────────────────────────────────────────────
# Phase 3 Work unit 2 — $ref scanning
# ────────────────────────────────────────────────────────────────────────

def test_compiler_same_group_ref_emits_no_extra_edge():
    """$ref to a call in the SAME parallel_group is handled by the existing
    group-order join edges; no additional edge is needed."""
    plan = FakePlan([
        FakeCall("get_quote", {"x": "literal"}, parallel_group=0),
        FakeCall("get_fundamentals", {"ref": "$data.quote"}, parallel_group=0),
    ])
    spec = PlanCompiler().compile(plan)
    # Both calls are data_agent → both are in group 0.
    # The same-group ref does NOT produce an extra data edge between the
    # two call nodes; only the group-join edges (call_X → join_0) exist.
    call_to_call_edges = [
        e for e in spec.edges
        if e.src.startswith("call_") and e.dst.startswith("call_")
    ]
    assert call_to_call_edges == [], (
        f"same-group $ref must NOT produce call-to-call edges; "
        f"got {[(e.src, e.dst, e.kind) for e in call_to_call_edges]}"
    )


def test_compiler_adjacent_upstream_ref_emits_data_edge():
    """$ref to a call in the IMMEDIATELY PREVIOUS group is a forward dep
    → emit data edge from referenced node to current node."""
    plan = FakePlan([
        FakeCall("get_quote", {"symbol": "X"}, parallel_group=0),  # data_agent
        FakeCall("compute_alpha_factors", {"q": "$data.x"}, parallel_group=1),  # alpha_agent
    ])
    spec = PlanCompiler().compile(plan)
    data_edges = [
        e for e in spec.edges
        if e.src.startswith("call_") and e.dst.startswith("call_")
        and e.kind == "data"
    ]
    assert len(data_edges) == 1, (
        f"expected 1 call-to-call data edge; got "
        f"{[(e.src, e.dst, e.kind) for e in data_edges]}"
    )
    assert data_edges[0].src == "call_0"
    assert data_edges[0].dst == "call_1"


def test_compiler_skip_upstream_ref_emits_loop_edge_with_max_hops_2():
    """$ref to a call in an EARLIER (but NOT immediately previous) group
    is a back-reference → emit loop edge with max_hops=2 per spec §4.6 step 4."""
    plan = FakePlan([
        FakeCall("get_quote", {"symbol": "X"}, parallel_group=0),       # data_agent
        FakeCall("compute_alpha_factors", {"symbol": "X"}, parallel_group=1),  # alpha_agent, no ref
        FakeCall("evaluate_alpha", {"q": "$data.x"}, parallel_group=2),       # alpha_agent refs $data (skips group 1)
    ])
    spec = PlanCompiler().compile(plan)
    loop_edges = [e for e in spec.edges if e.kind == "loop"]
    assert len(loop_edges) == 1, (
        f"expected 1 loop edge; got "
        f"{[(e.src, e.dst, e.kind, e.max_hops) for e in loop_edges]}"
    )
    edge = loop_edges[0]
    # Loop edge goes from current (call_2) back to referenced (call_0)
    assert edge.src == "call_2"
    assert edge.dst == "call_0"
    assert edge.max_hops == 2


def test_compiler_ref_to_unknown_agent_raises_compile_error():
    """$ref to an agent that's not in any other call → CompileError
    (per-agent scoping per spec §4.5)."""
    plan = FakePlan([
        FakeCall("compute_alpha_factors", {"q": "$nonexistent.x"}, parallel_group=0),
    ])
    with pytest.raises(CompileError, match="nonexistent"):
        PlanCompiler().compile(plan)


def test_compiler_self_ref_raises_compile_error():
    """$ref to the SAME agent as the current call's owner → CompileError
    (cannot ref your own outputs mid-graph; spec §4.5)."""
    plan = FakePlan([
        # get_quote → data_agent; refs $data (self).
        FakeCall("get_quote", {"ref": "$data.x"}, parallel_group=0),
    ])
    with pytest.raises(CompileError, match=r"self|not allowed"):
        PlanCompiler().compile(plan)


def test_compiler_ref_to_downstream_group_raises_compile_error():
    """$ref to a call in a HIGHER-numbered group (downstream in time) →
    CompileError (per-agent scoping). The agent has not run yet."""
    plan = FakePlan([
        # call_0 in group 0 refs $data, but the only data_agent call is in group 1.
        FakeCall("compute_alpha_factors", {"q": "$data.x"}, parallel_group=0),
        FakeCall("get_quote", {"symbol": "X"}, parallel_group=1),  # data_agent downstream
    ])
    with pytest.raises(CompileError):
        PlanCompiler().compile(plan)


def test_compiler_multiple_refs_emit_multiple_edges():
    """Call with two $refs to two different upstream agents →
    both edges emitted (data edges when ref is in immediately previous group)."""
    plan = FakePlan([
        FakeCall("get_quote", {}, parallel_group=0),  # data_agent
        FakeCall("get_news", {}, parallel_group=0),   # news_agent
        FakeCall("compute_alpha_factors",
                 {"q": "$data.x", "n": "$news.y"}, parallel_group=1),  # alpha_agent refs both
    ])
    spec = PlanCompiler().compile(plan)
    call_to_call_data = [
        e for e in spec.edges
        if e.src.startswith("call_") and e.dst.startswith("call_")
        and e.kind == "data"
    ]
    assert len(call_to_call_data) == 2
    src_dst = {(e.src, e.dst) for e in call_to_call_data}
    assert ("call_0", "call_2") in src_dst  # data → alpha
    assert ("call_1", "call_2") in src_dst  # news → alpha


def test_compiler_ref_with_malformed_syntax_is_silently_ignored():
    """$ref strings that fail parse_ref regex (e.g., `$data.0field`,
    `$123.x`) are ignored — runtime resolves them as literal fallback
    per spec §4.5 graceful degradation."""
    plan = FakePlan([
        FakeCall("get_quote", {"bad": "$data.0field"}, parallel_group=0),
        FakeCall("get_fundamentals", {"also_bad": "$123.x"}, parallel_group=0),
    ])
    # Both calls in same group; malformed refs don't add edges anyway.
    # The compiler must NOT raise — the LLM gets a graceful literal
    # fallback at runtime.
    spec = PlanCompiler().compile(plan)
    # Sanity: 2 nodes, group-join edges only
    assert len([n for n in spec.nodes.values() if n.kind == NodeKind.TOOL]) == 2
