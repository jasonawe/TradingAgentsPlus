import pytest
from tradingagents.agent_harness.runtime.multi_agent.graph import (
    NodeKind, Edge, GraphSpec, BaseNode,
)

def test_node_kind_values():
    assert NodeKind.TOOL.value == "tool"
    assert NodeKind.LLM.value == "llm"
    assert NodeKind.SUBPLAN.value == "subplan"
    assert NodeKind.CONSULT.value == "consult"

def test_edge_validates_kind():
    with pytest.raises(ValueError, match="invalid edge kind"):
        Edge(src="a", dst="b", kind="bogus")  # type: ignore[arg-type]

def test_edge_default_max_hops_is_2_per_spec():
    # Spec §4.6 step 4: loop edges default to max_hops=2.
    e = Edge(src="a", dst="b", kind="loop")
    assert e.max_hops == 2


def test_edge_has_no_hops_used_attribute_phase2():
    """Phase 2 Work unit 5: ``Edge.hops_used`` removed (spec §4.7 strict).

    Per-edge loop counter lives on ``state.edge_hops[(src, dst)]``,
    not on the Edge instance. The executor's ``run()`` resets the state
    counter at the top of every invocation — no Edge mutation needed.
    """
    e = Edge(src="a", dst="b", kind="loop", max_hops=3)
    assert not hasattr(e, "hops_used"), (
        "Phase 2 migration incomplete: Edge.hops_used must be removed; "
        "use state.edge_hops[(src, dst)] instead"
    )

def test_edge_accepts_valid_kinds():
    for k in ("data", "when", "loop"):
        e = Edge(src="a", dst="b", kind=k)  # type: ignore[arg-type]
        assert e.kind == k

class _StubNode:
    id = "n1"
    agent_id = "data_agent"
    kind = NodeKind.TOOL

    def __init__(self):
        from tradingagents.agent_harness.runtime.multi_agent.state import FieldRef
        self.inputs: list[FieldRef] = []
        self.outputs: list[FieldRef] = []

    async def run(self, state, inbox):
        return []

def test_graph_spec_node_lookup():
    g = GraphSpec(nodes={"n1": _StubNode()}, edges=[])
    assert g.node("n1").id == "n1"

def test_graph_spec_to_dict_roundtrip():
    g = GraphSpec(
        nodes={"n1": _StubNode()},
        edges=[Edge(src="n1", dst="n2", kind="data")],
        entry="n1", exit="n2",
    )
    blob = g.to_dict()
    assert blob["entry"] == "n1"
    assert blob["exit"] == "n2"
    assert "n1" in blob["nodes"]
    assert any(e["kind"] == "data" for e in blob["edges"])
