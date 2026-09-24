"""Tests for runtime/multi_agent/nodes.py (Phase 1).

Per plan Task 7:
  - ToolNode dispatches via ToolPipeline and emits a Message
  - LLMNode / SubplanNode / ConsultNode raise NotImplementedError
  - ConsultNode.outputs is typed ``list[FieldRef]`` (empty list default)
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from tradingagents.agent_harness.runtime.multi_agent.nodes import (
    ConsultNode,
    LLMNode,
    SubplanNode,
    ToolNode,
)
from tradingagents.agent_harness.runtime.multi_agent.graph import NodeKind
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


def test_node_kind_attributes():
    assert ToolNode(id="a", agent_id="data_agent", tool_name="get_quote",
                    raw_args={"symbol": "X"}).kind == NodeKind.TOOL
    assert LLMNode(id="b", agent_id="data_agent",
                   system_prompt="...").kind == NodeKind.LLM
    assert SubplanNode(id="c", agent_id="trading_agents",
                       sub_graph=MagicMock()).kind == NodeKind.SUBPLAN
    assert ConsultNode(id="d", agent_id="data_agent",
                       target_agent="alpha_agent",
                       question="?").kind == NodeKind.CONSULT


def test_consult_node_outputs_is_list_of_field_refs():
    # rev.4 fix from LOW #12: previously typed as list[list[FieldRef]]
    node = ConsultNode(id="d", agent_id="data_agent",
                       target_agent="alpha_agent", question="?")
    assert isinstance(node.outputs, list)
    out: list[FieldRef] = node.outputs
    assert out == []


def test_tool_node_dispatches_via_pipeline(monkeypatch):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    captured = {}

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            captured["tool_name"] = tool_name
            captured["args"] = args
            captured["tool_context"] = tool_context
            return MagicMock(ok=True, result={"echoed": tool_name, "args": args})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    node = ToolNode(id="a", agent_id="data_agent",
                    tool_name="get_quote", raw_args={"symbol": "X"})
    state = GraphState(run_id="r", turn_id="t", intent="x")
    out = _run(node.run(state, inbox=[]))
    assert captured["tool_name"] == "get_quote"
    assert captured["args"] == {"symbol": "X"}
    assert len(out) == 1
    msg = out[0]
    assert msg.sender == "a"
    assert msg.receiver == "*"
    # TypedResult.data round-trips the pipeline result
    assert msg.payload.data == {"echoed": "get_quote",
                                "args": {"symbol": "X"}}


def test_llm_node_raises_not_implemented():
    node = LLMNode(id="b", agent_id="data_agent", system_prompt="...")
    state = GraphState(run_id="r", turn_id="t", intent="x")
    with pytest.raises(NotImplementedError):
        _run(node.run(state, inbox=[]))


def test_subplan_node_raises_not_implemented():
    node = SubplanNode(id="c", agent_id="trading_agents", sub_graph=MagicMock())
    state = GraphState(run_id="r", turn_id="t", intent="x")
    with pytest.raises(NotImplementedError):
        _run(node.run(state, inbox=[]))


def test_consult_node_raises_not_implemented():
    node = ConsultNode(id="d", agent_id="data_agent",
                       target_agent="alpha_agent", question="?")
    state = GraphState(run_id="r", turn_id="t", intent="x")
    with pytest.raises(NotImplementedError):
        _run(node.run(state, inbox=[]))
