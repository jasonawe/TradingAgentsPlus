"""Step 31 — declarative Workflow primitive.

Tests:
- Linear graph runs node-by-node and yields emitted events in order
- Conditional edges: only the first matching edge is taken
- Priority orders edges when multiple conditions match
- Halt short-circuits remaining nodes
- Explicit next_node bypasses edges
- Unknown node → workflow_error event
- Cycle detection via max_hops safety bound
- End-to-end example: classify → plan → synthesize
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _collect(wf, state):
    async def _go():
        out = []
        async for ev, payload in wf.run(state):
            out.append((ev, payload))
        return out
    return asyncio.run(_go())


# ------------------------------------------------------------------
# Linear graph
# ------------------------------------------------------------------
def test_linear_graph_runs_in_order():
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow, END,
    )

    async def a(state):
        state["log"] = state.get("log", []) + ["a"]
        return _N(state, emit=[("a_done", {"v": 1})])

    async def b(state):
        state["log"] = state.get("log", []) + ["b"]
        return _N(state, emit=[("b_done", {"v": 2})])

    wf = Workflow()
    wf.add_node(Node("a", a))
    wf.add_node(Node("b", b))
    wf.add_edge(Edge("a", "b"))
    wf.add_edge(Edge("b", END))
    wf.set_entry("a")
    events = _collect(wf, {})
    names = [e[0] for e in events]
    assert names == ["a_done", "b_done"]
    assert wf.executed() == ["a", "b"]


# ------------------------------------------------------------------
# Conditional edges
# ------------------------------------------------------------------
def test_conditional_edge_first_match_wins():
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow, END,
    )

    async def classify(state):
        state["intent"] = "quote"
        return _N(state)

    async def data_path(state):
        return _N(state, emit=[("data_done", {})])

    async def crud_path(state):
        return _N(state, emit=[("crud_done", {})])

    wf = Workflow()
    wf.add_node(Node("classify", classify))
    wf.add_node(Node("data", data_path))
    wf.add_node(Node("crud", crud_path))
    wf.add_edge(Edge("classify", "data",
                     condition=lambda s: s.get("intent") == "quote"))
    wf.add_edge(Edge("classify", "crud",
                     condition=lambda s: s.get("intent") == "crud"))
    wf.add_edge(Edge("data", END))
    wf.add_edge(Edge("crud", END))
    wf.set_entry("classify")
    events = _collect(wf, {})
    assert [e[0] for e in events] == ["data_done"]


def test_no_matching_edge_implicit_end():
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow,
    )

    async def start(state):
        return _N(state, emit=[("started", {})])

    wf = Workflow()
    wf.add_node(Node("start", start))
    # No edges at all → implicit END after start
    wf.set_entry("start")
    events = _collect(wf, {})
    assert [e[0] for e in events] == ["started"]


# ------------------------------------------------------------------
# Priority
# ------------------------------------------------------------------
def test_priority_orders_matching_edges():
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow, END,
    )

    async def start(state):
        return _N(state)

    async def default_path(state):
        return _N(state, emit=[("default", {})])

    async def high_priority(state):
        return _N(state, emit=[("high", {})])

    wf = Workflow()
    wf.add_node(Node("start", start))
    wf.add_node(Node("default", default_path))
    wf.add_node(Node("high", high_priority))
    # Both edges match (condition=True) but high has lower priority
    wf.add_edge(Edge("start", "default", priority=10))
    wf.add_edge(Edge("start", "high", priority=-1))
    wf.add_edge(Edge("default", END))
    wf.add_edge(Edge("high", END))
    wf.set_entry("start")
    events = _collect(wf, {})
    assert [e[0] for e in events] == ["high"]


# ------------------------------------------------------------------
# Halt / next_node
# ------------------------------------------------------------------
def test_halt_short_circuits():
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow, END,
    )

    async def a(state):
        return _N(state, emit=[("a_done", {})], halt=True)

    async def b(state):
        return _N(state, emit=[("b_done", {})])

    wf = Workflow()
    wf.add_node(Node("a", a))
    wf.add_node(Node("b", b))
    wf.add_edge(Edge("a", "b"))
    wf.add_edge(Edge("b", END))
    wf.set_entry("a")
    events = _collect(wf, {})
    assert [e[0] for e in events] == ["a_done"]


def test_explicit_next_node_bypasses_edges():
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow, END,
    )

    async def classify(state):
        if state.get("trivial"):
            return _N(state, next_node="trivial_ack")
        return _N(state, next_node="plan")

    async def plan(state):
        return _N(state, emit=[("planned", {})])

    async def trivial_ack(state):
        return _N(state, emit=[("trivial", {})])

    wf = Workflow()
    wf.add_node(Node("classify", classify))
    wf.add_node(Node("plan", plan))
    wf.add_node(Node("trivial_ack", trivial_ack))
    # Even though edges point classify → plan, we override via next_node
    wf.add_edge(Edge("classify", "plan"))
    wf.add_edge(Edge("plan", END))
    wf.add_edge(Edge("trivial_ack", END))
    wf.set_entry("classify")
    events = _collect(wf, {"trivial": True})
    assert [e[0] for e in events] == ["trivial"]


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------
def test_unknown_node_yields_workflow_error():
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow,
    )

    async def start(state):
        return _N(state, next_node="ghost")

    wf = Workflow()
    wf.add_node(Node("start", start))
    wf.set_entry("start")
    events = _collect(wf, {})
    assert events[0][0] == "workflow_error"
    assert "ghost" in events[0][1]["error"]


def test_handler_exception_yields_workflow_error():
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow, END,
    )

    async def boom(state):
        raise RuntimeError("kaboom")

    wf = Workflow()
    wf.add_node(Node("boom", boom))
    wf.add_edge(Edge("boom", END))
    wf.set_entry("boom")
    events = _collect(wf, {})
    assert events[0][0] == "workflow_error"
    assert "kaboom" in events[0][1]["error"]


def test_cycle_caught_by_max_hops():
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow,
    )

    async def a(state):
        state["n"] = state.get("n", 0) + 1
        return _N(state)

    async def b(state):
        return _N(state)

    wf = Workflow()
    wf.add_node(Node("a", a))
    wf.add_node(Node("b", b))
    # a → b → a (cycle!)
    wf.add_edge(Edge("a", "b"))
    wf.add_edge(Edge("b", "a"))
    wf.set_entry("a")
    events = _collect(wf, {})
    # Eventually we hit max_hops and yield workflow_error
    assert any(e[0] == "workflow_error" for e in events)


# ------------------------------------------------------------------
# Construction-time validation
# ------------------------------------------------------------------
def test_duplicate_node_id_raises():
    from tradingagents.agent_harness.core.workflow import Node, Workflow
    wf = Workflow()
    async def h(s): return _N(s)
    wf.add_node(Node("a", h))
    try:
        wf.add_node(Node("a", h))
    except ValueError:
        return
    assert False, "expected ValueError"


def test_set_entry_unknown_node_raises():
    from tradingagents.agent_harness.core.workflow import Node, Workflow
    wf = Workflow()
    async def h(s): return _N(s)
    wf.add_node(Node("a", h))
    try:
        wf.set_entry("ghost")
    except ValueError:
        return
    assert False, "expected ValueError"


# ------------------------------------------------------------------
# End-to-end example mimicking a mini orchestrator
# ------------------------------------------------------------------
def test_mini_orchestrator_workflow():
    """3-node pipeline: classify -> plan -> synthesize.

    Simulates the orchestrator's main flow at a tiny scale so we can
    see the primitive working end-to-end.
    """
    from tradingagents.agent_harness.core.workflow import (
        Node, Edge, Workflow, END,
    )

    async def classify(state):
        state["intent"] = "quote"
        state["symbols"] = ["AAPL"]
        return _N(state, emit=[("classified", {"intent": "quote"})])

    async def plan(state):
        steps = [{"action": "get_quote", "args": {"symbol": "AAPL"}}]
        state["plan"] = steps
        return _N(state, emit=[("plan_ready", {"steps": steps})])

    async def synthesize(state):
        result = state.get("tool_results") or [{"price": 185.0}]
        return _N(state, emit=[("agent_final", {"result": result})])

    wf = Workflow(name="mini-orchestrator")
    wf.add_node(Node("classify", classify))
    wf.add_node(Node("plan", plan))
    wf.add_node(Node("synthesize", synthesize))
    wf.add_edge(Edge("classify", "plan"))
    wf.add_edge(Edge("plan", "synthesize"))
    wf.add_edge(Edge("synthesize", END))
    wf.set_entry("classify")
    events = _collect(wf, {})
    names = [e[0] for e in events]
    assert names == ["classified", "plan_ready", "agent_final"]


# ------------------------------------------------------------------
# Helper used by tests above
# ------------------------------------------------------------------
def _N(state, *, emit=None, next_node=None, halt=False):
    from tradingagents.agent_harness.core.workflow import NodeResult
    return NodeResult(
        state=state,
        emit=emit or [],
        next_node=next_node,
        halt=halt,
    )
