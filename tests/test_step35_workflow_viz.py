"""Step 35 — workflow DOT/JSON rendering."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _wf():
    """Build a 3-node workflow with a named condition."""
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow,
    )

    async def step_a(state): pass
    async def step_b(state): pass
    async def step_c(state): pass

    def cond(state): return True

    wf = Workflow(name="test-viz")
    wf.add_node(Node("a", step_a))
    wf.add_node(Node("b", step_b))
    wf.add_node(Node("c", step_c))
    wf.add_edge(Edge("a", "b", condition=cond, priority=0))
    wf.add_edge(Edge("a", "c"))
    wf.add_edge(Edge("b", "<end>"))
    wf.add_edge(Edge("c", "<end>"))
    wf.set_entry("a")
    return wf


def test_to_dot_includes_workflow_name_and_direction():
    from tradingagents.agent_harness.core.workflow_viz import to_dot
    dot = to_dot(_wf())
    assert dot.startswith("digraph test-viz {")
    assert "rankdir=LR" in dot
    assert dot.rstrip().endswith("}")


def test_to_dot_includes_all_nodes_with_handlers():
    from tradingagents.agent_harness.core.workflow_viz import to_dot
    dot = to_dot(_wf())
    assert '"a" [label="a' in dot  # node label includes handler
    assert 'step_a' in dot
    assert '"b"' in dot
    assert '"c"' in dot


def test_to_dot_includes_end_sentinel():
    from tradingagents.agent_harness.core.workflow_viz import to_dot
    dot = to_dot(_wf())
    assert 'doublecircle' in dot
    assert '"<end>"' in dot


def test_to_dot_labels_conditional_edges():
    from tradingagents.agent_harness.core.workflow_viz import to_dot
    dot = to_dot(_wf())
    # Edge a->b has a named condition "cond" — should show label
    assert '"a" -> "b" [label="cond"]' in dot
    # Edge a->c has no condition — should have no label
    assert '"a" -> "c";' in dot


def test_to_json_structure():
    from tradingagents.agent_harness.core.workflow_viz import to_json
    j = to_json(_wf())
    assert j["name"] == "test-viz"
    assert j["entry"] == "a"
    ids = [n["id"] for n in j["nodes"]]
    assert ids == ["a", "b", "c", "<end>"]
    # End sentinel marked terminal
    assert j["nodes"][-1]["terminal"] is True
    pairs = [(e["from"], e["to"]) for e in j["edges"]]
    assert ("a", "b") in pairs
    assert ("a", "c") in pairs
    assert ("b", "<end>") in pairs
    assert ("c", "<end>") in pairs


def test_to_json_labels():
    from tradingagents.agent_harness.core.workflow_viz import to_json
    j = to_json(_wf())
    by_pair = {(e["from"], e["to"]): e["label"] for e in j["edges"]}
    assert by_pair[("a", "b")] == "cond"
    assert by_pair[("a", "c")] == "always"


def test_to_json_includes_executed_log():
    """Executed log shows the actual node visit order."""
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow, NodeResult,
    )
    from tradingagents.agent_harness.core.workflow_viz import to_json
    import asyncio

    async def step_a(state):
        return NodeResult(state, emit=[], halt=False)

    async def step_b(state):
        return NodeResult(state, emit=[], halt=True)

    wf = Workflow(name="exec-log")
    wf.add_node(Node("a", step_a))
    wf.add_node(Node("b", step_b))
    wf.add_edge(Edge("a", "b"))
    wf.add_edge(Edge("b", "<end>"))
    wf.set_entry("a")

    async def _run():
        async for _ in wf.run({}):
            pass
    asyncio.run(_run())
    j = to_json(wf)
    assert j["executed"] == ["a", "b"]


def test_to_dot_handles_empty_workflow():
    from tradingagents.agent_harness.core.workflow import Workflow
    from tradingagents.agent_harness.core.workflow_viz import to_dot
    # Workflow with no nodes + no entry should still render
    # (the to_dot is only called after construction; if entry is
    # missing Workflow.run raises. We only test the render here.)
    wf = Workflow(name="empty")
    dot = to_dot(wf)
    assert "digraph empty" in dot
    assert '"<end>"' in dot  # sentinel always rendered
