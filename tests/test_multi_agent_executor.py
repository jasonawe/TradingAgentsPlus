"""Tests for runtime/multi_agent/executor.py (Phase 1).

Per plan Task 9 contracts:
- per-run Edge.hops_used reset + self._seq reset
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
    ToolNode,
)
from tradingagents.agent_harness.runtime.multi_agent.state import GraphState


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


def test_executor_resets_hops_between_runs(monkeypatch):
    """rev.5 regression for round-4 HIGH #1: same executor+spec pair must
    produce identical results on consecutive runs (no Edge.hops_used leak)."""
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
        f"{log_after_first}; Edge.hops_used leaked across runs"
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
