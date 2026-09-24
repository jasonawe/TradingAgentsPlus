"""Tests for runtime/multi_agent/executor.py (Phase 1).

Per plan Task 9 contracts:
- per-run state.edge_hops reset + self._seq reset
- inbox propagation across edges
- budget guard (no LLM fires past budget)
- NotImplementedError swallow for LLMNode AND ConsultNode stubs (no budget burn)
- loop edge re-executes upstream up to max_hops
- cross-run isolation (same executor+spec produces identical results twice)
- heartbeat logs at start + finish
"""
import asyncio
import logging
from unittest.mock import MagicMock

from tradingagents.agent_harness.runtime.multi_agent.executor import GraphExecutor
from tradingagents.agent_harness.runtime.multi_agent.graph import (
    Edge,
    GraphSpec,
)
from tradingagents.agent_harness.runtime.multi_agent.nodes import (
    ConsultNode,
    LLMNode,
    SubplanNode,
    ToolNode,
)
from tradingagents.agent_harness.runtime.multi_agent.state import (
    FieldRef,
    GraphState,
)


# Repo convention (no pytest-asyncio): sync wrapper that drives asyncio.run.
# The plan literally wrote ``async def _run(coro): return await coro`` but
# calling that from a sync ``def test_*`` returns an un-awaited coroutine —
# the inner pipeline / NotImplementedError never runs. Fix: use asyncio.run.
def _run(coro):
    return asyncio.run(coro)


def _graph_two_tool_nodes():
    n1 = ToolNode(id="a", agent_id="data_agent",
                  tool_name="get_quote", raw_args={"symbol": "X"})
    n2 = ToolNode(id="b", agent_id="data_agent",
                  tool_name="get_fundamentals", raw_args={"symbol": "X"})
    return GraphSpec(
        nodes={"a": n1, "b": n2},
        edges=[Edge(src="a", dst="b", kind="data")],
        entry="a", exit="b",
    )


def _graph_self_loop():
    """1 ToolNode with a self-loop edge — used for loop regression."""
    n1 = ToolNode(id="a", agent_id="data_agent",
                  tool_name="get_quote", raw_args={"symbol": "X"})
    return GraphSpec(
        nodes={"a": n1},
        edges=[Edge(src="a", dst="a", kind="loop", max_hops=2)],
        entry="a", exit="a",
    )


def test_executor_walks_linear_graph(monkeypatch):
    """rev.4 fix from HIGH #5: only 1 message_log entry for 1 cross-edge."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x")
    out = _run(GraphExecutor().run(_graph_two_tool_nodes(), state))
    assert out.intent == "x"
    # Only ONE fan-out call fires (a -> b). So message_log has exactly 1.
    assert len(state.message_log) == 1


def test_executor_populates_downstream_inbox(monkeypatch):
    """regression for round-2 BLOCKER #2."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x")
    _run(GraphExecutor().run(_graph_two_tool_nodes(), state))

    cross_messages = [
        m for m in state.message_log
        if m.receiver == "b" and m.sender == "a"
    ]
    assert len(cross_messages) >= 1


def test_executor_budget_guard(monkeypatch):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x",
                       llm_used=5, budget_limit=5)
    state.hops_remaining = 8
    out = _run(GraphExecutor().run(_graph_two_tool_nodes(), state))
    assert out.llm_used == 5


def test_executor_swallows_not_implemented_for_llm_node(monkeypatch):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    n1 = LLMNode(id="a", agent_id="data_agent", system_prompt="...")
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(run_id="r", turn_id="t", intent="x")
    out = _run(GraphExecutor().run(spec, state))
    assert out.intent == "x"
    assert out.llm_used == 0


def test_executor_swallows_not_implemented_for_consult_node(monkeypatch):
    """Phase 1: ConsultNode stub raised NotImplementedError; executor
    caught + never bumped state.consultation_used. Phase 2 (Work unit 3):
    ConsultNode is wired through ToolPipeline → consult_subagent. This
    test stubs the pipeline to return ``MagicMock(ok=True, result={})``
    WITHOUT invoking the executor, so consult_subagent never runs and
    consultation_used stays 0. The Phase 1 invariant
    (consultation_used unchanged) is preserved by construction — the
    trigger is no longer an exception swallow but pipeline-stage bypass."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    n1 = ConsultNode(id="a", agent_id="data_agent",
                     target_agent="alpha_agent", question="?")
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(run_id="r", turn_id="t", intent="x")
    out = _run(GraphExecutor().run(spec, state))
    assert out.intent == "x"
    assert out.consultation_used == 0, (
        "ConsultNode stub must not consume consultation_used on NotImplementedError"
    )


def test_executor_loop_edge_re_executes_upstream(monkeypatch):
    """rev.4/5 regression for loop edges: max_hops=2 → loop fires twice,
    each fire creates 2 msgs (new_msg + loop_msg). Total = 1 (initial fan-out)
    + 4 (2 fires × 2 msgs) = 4 messages in message_log.

    Trace (per plan docstring):
    - iter 1 (hops 8→7): pop bootstrap. Run → 1 out_msg. Loop fires (0→1).
      append_inbox(new_msg_1), append_inbox(loop_msg_1). Log=2.
    - iter 2 (hops 7→6): pop new_msg_1. Run → 1 out_msg. Loop fires (1→2).
      append_inbox(new_msg_2), append_inbox(loop_msg_2). Log=4.
    - iters 3-5: pop remaining queued msgs; runs happen but loop NOT firing
      (hops_used == max_hops), so message_log stays at 4.
    """
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    spec = _graph_self_loop()
    state = GraphState(run_id="r", turn_id="t", intent="x")
    _run(GraphExecutor().run(spec, state))

    assert len(state.message_log) == 4, (
        f"expected 4 messages (initial fan-out + 2 loop fires × 2 msgs); "
        f"got {len(state.message_log)}"
    )
    # Phase 2 (Work unit 5): per-edge loop counter lives on state, NOT Edge.
    # After 2 fires (max_hops=2), the self-loop edge has counter 2.
    assert state.edge_hops == {("a", "a"): 2}


def test_executor_state_edge_hops_resets_per_run(monkeypatch):
    """Phase 2 Work unit 5: ``run()`` resets ``state.edge_hops = {}`` at
    the top so the same executor+spec pair is reusable across turns."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    # Pre-populate state.edge_hops with stale values from a prior run.
    # The executor must clear them at the top of run().
    state = GraphState(run_id="r", turn_id="t", intent="x")
    state.edge_hops = {("stale", "edge"): 99}

    spec = _graph_self_loop()
    _run(GraphExecutor().run(spec, state))

    # Stale entry cleared; only fresh entries from this run remain.
    assert ("stale", "edge") not in state.edge_hops
    assert state.edge_hops == {("a", "a"): 2}


def test_executor_state_edge_hops_starts_empty():
    """Phase 2 Work unit 5: default state.edge_hops is an empty dict."""
    state = GraphState(run_id="r", turn_id="t", intent="x")
    assert state.edge_hops == {}
    assert isinstance(state.edge_hops, dict)


def test_executor_resets_hops_between_runs(monkeypatch):
    """Phase 2 (Work unit 5) regression for round-4 HIGH #1: same
    executor+spec pair must produce identical results on consecutive
    runs (no state.edge_hops leak between turns)."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    spec = _graph_self_loop()
    state1 = GraphState(run_id="r1", turn_id="t1", intent="x")
    state2 = GraphState(run_id="r2", turn_id="t2", intent="x")
    executor = GraphExecutor()

    _run(executor.run(spec, state1))
    log_after_first = len(state1.message_log)

    _run(executor.run(spec, state2))
    log_after_second = len(state2.message_log)

    assert log_after_first == log_after_second, (
        f"second run produced {log_after_second} messages vs first run's "
        f"{log_after_first}; state.edge_hops leaked across runs"
    )


def test_executor_emits_heartbeat_logs(monkeypatch, caplog):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    caplog.set_level(
        logging.INFO,
        logger="tradingagents.agent_harness.runtime.multi_agent.executor",
    )
    state = GraphState(run_id="r", turn_id="t", intent="x")
    _run(GraphExecutor().run(_graph_two_tool_nodes(), state))

    start_logs = [r for r in caplog.records if "GraphExecutor starting" in r.message]
    end_logs = [r for r in caplog.records if "GraphExecutor finished" in r.message]
    assert len(start_logs) >= 1
    assert len(end_logs) >= 1


# ----------------------------------------------------------------------
# §0.4.35 phase 2 — Work unit 4: consultation_depth guard
# ----------------------------------------------------------------------

def _graph_single_consult():
    """1 ConsultNode, no edges."""
    n1 = ConsultNode(id="a", agent_id="data_agent",
                     target_agent="alpha_agent", question="?")
    return GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")


def test_executor_refuses_consult_when_depth_at_max():
    """state.consultation_depth == consultation_max_depth → refusal,
    ok=False result, no consultation_used bump, no depth increment."""
    n1 = ConsultNode(id="a", agent_id="data_agent",
                     target_agent="alpha_agent", question="?")
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(
        run_id="r", turn_id="t", intent="x",
        consultation_depth=3,  # already at max (default)
    )

    out = _run(GraphExecutor(consultation_max_depth=3).run(spec, state))

    assert len(state.message_log) == 1
    msg = state.message_log[0]
    assert msg.payload.data["ok"] is False
    assert msg.payload.data["error"] == "ConsultDepthExceeded"
    # consultation_used NOT bumped (consult_subagent never invoked)
    assert out.consultation_used == 0
    # depth NOT incremented (refusal path)
    assert state.consultation_depth == 3


def test_executor_refuses_consult_when_depth_exceeds_max():
    """state.consultation_depth > consultation_max_depth → same refusal."""
    n1 = ConsultNode(id="a", agent_id="data_agent",
                     target_agent="alpha_agent", question="?")
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(
        run_id="r", turn_id="t", intent="x",
        consultation_depth=4,  # past max
    )

    out = _run(GraphExecutor(consultation_max_depth=3).run(spec, state))

    assert len(state.message_log) == 1
    assert out.consultation_used == 0
    assert state.consultation_depth == 4  # unchanged


def test_executor_consult_depth_increments_and_decrements(monkeypatch):
    """ConsultNode.run fires → depth goes 0→1 inside the call, back to 0 after."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class _InspectingPipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            # Inspect depth at the moment ConsultNode.run is awaiting the pipeline
            assert tool_context.extra["graph_state"].consultation_depth == 1, (
                "executor must increment consultation_depth before "
                "delegating to ConsultNode.run"
            )
            return MagicMock(ok=True, result={"answer": "x"})

    monkeypatch.setattr(nodes_mod, "_default_pipeline",
                        lambda: _InspectingPipeline())

    spec = _graph_single_consult()
    state = GraphState(run_id="r", turn_id="t", intent="x")

    _run(GraphExecutor(consultation_max_depth=3).run(spec, state))

    # Depth restored to 0 after the consult completes
    assert state.consultation_depth == 0


def test_executor_consult_depth_decrements_even_on_consult_node_failure(monkeypatch):
    """If ConsultNode.run raises, the finally clause restores depth to 0."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class _BoomPipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            raise RuntimeError("simulated consult pipeline failure")

    monkeypatch.setattr(nodes_mod, "_default_pipeline",
                        lambda: _BoomPipeline())

    spec = _graph_single_consult()
    state = GraphState(run_id="r", turn_id="t", intent="x")

    # Executor swallows node exceptions (rev.5 contract); depth must be restored.
    _run(GraphExecutor(consultation_max_depth=3).run(spec, state))

    assert state.consultation_depth == 0, (
        "depth must be decremented in finally even when ConsultNode.run raises"
    )


def test_graph_state_consultation_depth_defaults_to_zero():
    """Fresh per-turn state starts at depth=0 (auto-reset across turns)."""
    state = GraphState(run_id="r", turn_id="t", intent="x")
    assert state.consultation_depth == 0


def test_graph_state_consultation_depth_resets_per_state():
    """Two independent GraphState instances do not share depth counter."""
    s1 = GraphState(run_id="r", turn_id="t1", intent="x",
                    consultation_depth=5)
    s2 = GraphState(run_id="r", turn_id="t2", intent="x")
    assert s1.consultation_depth == 5  # explicit override preserved
    assert s2.consultation_depth == 0  # fresh state independent


# ----------------------------------------------------------------------
# §0.4.35 phase 3 — Work unit 1: state.agent_outputs[node.id] = result
# ----------------------------------------------------------------------

def test_executor_writes_tool_node_result_to_agent_outputs(monkeypatch):
    """ToolNode happy path → state.agent_outputs["a"] is the result's TypedResult."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name, "args": args})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    n1 = ToolNode(id="a", agent_id="data_agent",
                  tool_name="get_quote", raw_args={"symbol": "X"})
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(run_id="r", turn_id="t", intent="x")

    _run(GraphExecutor().run(spec, state))

    assert "a" in state.agent_outputs
    typed = state.agent_outputs["a"]
    assert typed.data == {"echoed": "get_quote", "args": {"symbol": "X"}}
    assert typed.meta["source_agent"] == "data_agent"


def test_executor_writes_llm_node_result_to_agent_outputs(monkeypatch):
    """LLMNode happy path → state.agent_outputs["a"] is the answer TypedResult."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    fake_provider = MagicMock()
    fake_provider.complete.return_value = MagicMock(content="the answer")
    n1 = LLMNode(id="a", agent_id="data_agent", system_prompt="...")
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(
        run_id="r", turn_id="t", intent="x",
        llm_provider=fake_provider,
    )

    _run(GraphExecutor().run(spec, state))

    assert "a" in state.agent_outputs
    typed = state.agent_outputs["a"]
    assert typed.data["answer"] == "the answer"
    assert typed.data["llm_used"] == 1
    assert typed.meta["source_agent"] == "data_agent"


def test_executor_writes_consult_node_result_to_agent_outputs(monkeypatch):
    """ConsultNode happy path → executor writes the canonical TypedResult
    (last-write-wins; overwrites ConsultNode's internal write)."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"answer": "consult answer"})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    n1 = ConsultNode(id="a", agent_id="data_agent",
                     target_agent="alpha_agent", question="?")
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(run_id="r", turn_id="t", intent="x")

    _run(GraphExecutor().run(spec, state))

    assert "a" in state.agent_outputs
    typed = state.agent_outputs["a"]
    # The executor's write uses ConsultNode's out_messages[-1].payload shape
    assert typed.data["ok"] is True
    assert typed.data["answer"] == "consult answer"


def test_executor_writes_failure_marker_for_subplan_node():
    """SubplanNode raises NotImplementedError → executor writes ok=False marker."""
    n1 = SubplanNode(id="a", agent_id="data_agent", sub_graph=MagicMock())
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(run_id="r", turn_id="t", intent="x")

    # Executor swallows the exception (rev.5 contract); agent_outputs must
    # still record the failure for downstream $ref to detect.
    _run(GraphExecutor().run(spec, state))

    assert "a" in state.agent_outputs
    typed = state.agent_outputs["a"]
    assert typed.data is None
    assert typed.meta["ok"] is False
    assert "NotImplementedError" in typed.meta["error"]
    assert typed.meta["source_agent"] == "data_agent"


def test_executor_writes_failure_marker_for_failed_llm_node_no_provider(monkeypatch):
    """LLMNode raises NotImplementedError when no provider wired → marker written."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    n1 = LLMNode(id="a", agent_id="data_agent", system_prompt="...")
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(run_id="r", turn_id="t", intent="x", llm_provider=None)

    _run(GraphExecutor().run(spec, state))

    assert "a" in state.agent_outputs
    typed = state.agent_outputs["a"]
    assert typed.data is None
    assert typed.meta["ok"] is False
    assert "NotImplementedError" in typed.meta["error"]


def test_executor_writes_refused_marker_for_depth_exceeded_consult():
    """ConsultNode refused by depth guard → executor writes the refused TypedResult."""
    n1 = ConsultNode(id="a", agent_id="data_agent",
                     target_agent="alpha_agent", question="?")
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(
        run_id="r", turn_id="t", intent="x",
        consultation_depth=3,  # at max
    )

    _run(GraphExecutor(consultation_max_depth=3).run(spec, state))

    assert "a" in state.agent_outputs
    typed = state.agent_outputs["a"]
    assert typed.data["ok"] is False
    assert typed.data["error"] == "ConsultDepthExceeded"
    assert typed.meta["refused"] is True


def test_executor_agent_outputs_isolated_between_runs(monkeypatch):
    """Two independent GraphState instances do not share agent_outputs."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name, "args": args})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    n1 = ToolNode(id="a", agent_id="data_agent",
                  tool_name="get_quote", raw_args={"symbol": "X"})
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")

    s1 = GraphState(run_id="r", turn_id="t1", intent="x")
    s2 = GraphState(run_id="r", turn_id="t2", intent="x")

    _run(GraphExecutor().run(spec, s1))
    _run(GraphExecutor().run(spec, s2))

    expected = {"echoed": "get_quote", "args": {"symbol": "X"}}
    assert s1.agent_outputs["a"].data == expected
    assert s2.agent_outputs["a"].data == expected
    # Both states are independently populated (no shared dict)
    assert s1 is not s2
    assert s1.agent_outputs is not s2.agent_outputs


# ----------------------------------------------------------------------
# §0.4.35 phase 3 — Work unit 3: FieldRef-satisfaction activation rule
# ----------------------------------------------------------------------

def test_is_activated_no_inputs_returns_true():
    """A node with empty inputs is activated unconditionally (Phase 1 semantic)."""
    n1 = ToolNode(id="a", agent_id="data_agent",
                  tool_name="get_quote", raw_args={"symbol": "X"})
    state = GraphState(run_id="r", turn_id="t", intent="x")
    # FieldRef-satisfaction path: _is_activated called directly
    assert GraphExecutor()._is_activated(n1, state) is True


def test_is_activated_fieldref_input_returns_false_when_unsatisfied():
    """A node with $ref input that has no upstream output is NOT activated."""
    from tradingagents.agent_harness.runtime.multi_agent.nodes import ConsultNode
    n1 = ConsultNode(
        id="c", agent_id="data_agent",
        target_agent="alpha_agent", question="?",
        inputs=[FieldRef(agent="data", field="x")],
    )
    state = GraphState(run_id="r", turn_id="t", intent="x")
    # No state.agent_outputs["data"] yet → not activated
    assert GraphExecutor()._is_activated(n1, state) is False


def test_is_activated_fieldref_input_returns_true_when_satisfied():
    """A node with $ref input activates once upstream populated agent_outputs."""
    from tradingagents.agent_harness.runtime.multi_agent.nodes import ConsultNode
    from tradingagents.agent_harness.runtime.multi_agent.state import TypedResult
    n1 = ConsultNode(
        id="c", agent_id="data_agent",
        target_agent="alpha_agent", question="?",
        inputs=[FieldRef(agent="data", field="x")],
    )
    state = GraphState(run_id="r", turn_id="t", intent="x")
    state.agent_outputs["data"] = TypedResult(
        schema=dict, data={"x": 42}, meta={"source_ts": 0.0},
    )
    assert GraphExecutor()._is_activated(n1, state) is True


def test_is_activated_failure_marker_treated_as_unsatisfied():
    """Failure marker (data=None, ok=False) does NOT satisfy the $ref."""
    from tradingagents.agent_harness.runtime.multi_agent.nodes import ConsultNode
    from tradingagents.agent_harness.runtime.multi_agent.state import TypedResult
    n1 = ConsultNode(
        id="c", agent_id="data_agent",
        target_agent="alpha_agent", question="?",
        inputs=[FieldRef(agent="data", field="x")],
    )
    state = GraphState(run_id="r", turn_id="t", intent="x")
    state.agent_outputs["data"] = TypedResult(
        schema=dict, data=None, meta={"ok": False, "error": "boom"},
    )
    assert GraphExecutor()._is_activated(n1, state) is False


def test_is_activated_multiple_refs_requires_all_satisfied():
    """A node with multiple $ref inputs only activates when ALL are satisfied."""
    from tradingagents.agent_harness.runtime.multi_agent.nodes import ConsultNode
    from tradingagents.agent_harness.runtime.multi_agent.state import TypedResult
    n1 = ConsultNode(
        id="c", agent_id="data_agent",
        target_agent="alpha_agent", question="?",
        inputs=[
            FieldRef(agent="data", field="x"),
            FieldRef(agent="news", field="y"),
        ],
    )
    state = GraphState(run_id="r", turn_id="t", intent="x")
    # Only "data" populated
    state.agent_outputs["data"] = TypedResult(
        schema=dict, data={"x": 1}, meta={},
    )
    assert GraphExecutor()._is_activated(n1, state) is False
    # Now both
    state.agent_outputs["news"] = TypedResult(
        schema=dict, data={"y": 2}, meta={},
    )
    assert GraphExecutor()._is_activated(n1, state) is True


def test_executor_skips_node_with_unsatisfied_fieldref_inputs(monkeypatch):
    """Spec §4.7: ConsultNode with $data.x ref does NOT run until data_agent
    has populated state.agent_outputs["data"]."""
    from tradingagents.agent_harness.runtime.multi_agent.nodes import ConsultNode
    from tradingagents.agent_harness.runtime.multi_agent.state import (
        TypedResult, Message, FieldRef,
    )

    consult_calls = []

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            consult_calls.append((tool_name, args))
            return MagicMock(ok=True, result={"answer": "stub"})

    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod
    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    # Graph: a (ToolNode) → c (ConsultNode with $data ref).
    # But: node id "a" is NOT named "data" — FieldRef(agent="data") needs
    # agent_outputs["data"]. So we need a graph where one node's id is "data".
    n_data = ToolNode(id="data", agent_id="data_agent",
                      tool_name="get_quote", raw_args={"symbol": "X"})
    n_c = ConsultNode(
        id="c", agent_id="data_agent",
        target_agent="alpha_agent", question="?",
        inputs=[FieldRef(agent="data", field="x")],
    )
    spec = GraphSpec(
        nodes={"data": n_data, "c": n_c},
        edges=[Edge(src="data", dst="c", kind="data")],
        entry="data", exit="c",
    )
    state = GraphState(run_id="r", turn_id="t", intent="x")

    _run(GraphExecutor().run(spec, state))

    # n_c ran exactly once — AFTER n_data populated agent_outputs["data"].
    # Filter to consult_subagent pipeline calls (ToolNode also uses the
    # pipeline with tool_name="get_quote").
    consult_subagent_calls = [
        c for c in consult_calls if c[0] == "consult_subagent"
    ]
    assert len(consult_subagent_calls) == 1, (
        "ConsultNode must run after upstream populates state.agent_outputs[ref.agent]; "
        f"got {len(consult_subagent_calls)} consult_subagent calls"
    )


def test_executor_loop_edge_still_re_fires_within_max_hops(monkeypatch):
    """Loop semantics preserved after activation-rule swap: self-loop with
    max_hops=2 produces 2 loop fires (each = 2 messages = 4 total)."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    n1 = ToolNode(id="a", agent_id="data_agent",
                  tool_name="get_quote", raw_args={"symbol": "X"})
    spec = GraphSpec(
        nodes={"a": n1},
        edges=[Edge(src="a", dst="a", kind="loop", max_hops=2)],
        entry="a", exit="a",
    )
    state = GraphState(run_id="r", turn_id="t", intent="x")

    _run(GraphExecutor().run(spec, state))

    assert len(state.message_log) == 4, (
        f"loop semantics broken by activation swap: expected 4, got {len(state.message_log)}"
    )
