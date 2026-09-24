from tradingagents.agent_harness.runtime.multi_agent.state import (
    GraphState, TypedResult, Message, FieldRef,
)

def test_typed_result_roundtrip():
    class Quote:
        def __init__(self, price): self.price = price
    res = TypedResult(schema=Quote, data=Quote(41.06), meta={"source_ts": 1.0})
    assert res.data.price == 41.06
    assert res.meta["source_ts"] == 1.0

def test_message_defaults():
    m = Message(sender="a", receiver="b",
                payload=TypedResult(schema=dict, data={}, meta={}))
    assert m.kind == "data"
    assert m.hop == 0

def test_fieldref_str():
    r = FieldRef(agent="data", field="quote.symbol")
    assert str(r) == "\$data.quote.symbol"

def test_graph_state_append_inbox_deposits_and_logs():
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    msg = Message(sender="x", receiver="y",
                  payload=TypedResult(schema=dict, data={"a": 1}, meta={}))
    state.append_inbox(msg)
    assert state.inbox["y"] == [msg]
    assert state.message_log == [msg]

def test_graph_state_consume_inbox_is_idempotent():
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    msg = Message(sender="x", receiver="y",
                  payload=TypedResult(schema=dict, data={"a": 1}, meta={}))
    state.append_inbox(msg)
    inbox = state.consume_inbox("y")
    assert len(inbox) == 1
    assert state.consume_inbox("y") == []


# ────────────────────────────────────────────────────────────────────────
# §0.4.35 phase 4 — Work unit 1: GraphState.fork() + subplan_depth
# ────────────────────────────────────────────────────────────────────────

import copy as _copy

def test_graph_state_subplan_depth_defaults_to_zero():
    """Phase 4 WU1: subplan_depth is a new per-turn counter, default 0."""
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    assert state.subplan_depth == 0


def test_graph_state_subplan_max_depth_default_is_three():
    """Phase 4 WU1: subplan_max_depth default = 3 (mirrors consultation_max_depth)."""
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    assert state.subplan_max_depth == 3


def test_graph_state_fork_is_independent_for_mutable_fields():
    """Phase 4 WU1: mutating fork's mutable fields does NOT affect parent.

    Covers agent_outputs, inbox, message_log, edge_hops."""
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    state.agent_outputs["data"] = TypedResult(
        schema=dict, data={"x": [1, 2, 3]}, meta={"source_ts": 1.0},
    )
    msg = Message(sender="x", receiver="y",
                  payload=TypedResult(schema=dict, data={"a": 1}, meta={}))
    state.append_inbox(msg)
    state.edge_hops[("a", "b")] = 1

    forked = state.fork()

    # Mutate the fork
    forked.agent_outputs["data"].data["x"].append(99)
    forked.inbox["y"].append(
        Message(sender="z", receiver="y",
                payload=TypedResult(schema=dict, data={"b": 2}, meta={}))
    )
    forked.message_log.append(
        Message(sender="z", receiver="y",
                payload=TypedResult(schema=dict, data={"b": 2}, meta={}))
    )
    forked.edge_hops[("a", "b")] = 99
    forked.edge_hops[("c", "d")] = 5

    # Parent unchanged
    assert state.agent_outputs["data"].data["x"] == [1, 2, 3]
    assert len(state.inbox["y"]) == 1
    assert len(state.message_log) == 1
    assert state.edge_hops == {("a", "b"): 1}


def test_graph_state_fork_shares_scalar_fields():
    """Phase 4 WU1: scalars (run_id / turn_id / intent / counters) share
    same value — fork() is structural copy, not semantic identity."""
    state = GraphState(
        run_id="r1", turn_id="t1", intent="analysis",
        hops_remaining=4, llm_used=2, consultation_used=1,
        consultation_depth=2, subplan_depth=1,
    )
    forked = state.fork()
    assert forked.run_id == "r1"
    assert forked.turn_id == "t1"
    assert forked.intent == "analysis"
    assert forked.hops_remaining == 4
    assert forked.llm_used == 2
    assert forked.consultation_used == 1
    assert forked.consultation_depth == 2
    # fork() preserves subplan_depth (SubplanNode.run explicitly bumps it).
    assert forked.subplan_depth == 1


def test_graph_state_fork_returns_graphstate_instance():
    """Phase 4 WU1: fork returns GraphState, not a bare dict."""
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    forked = state.fork()
    assert isinstance(forked, GraphState)
    assert forked is not state
