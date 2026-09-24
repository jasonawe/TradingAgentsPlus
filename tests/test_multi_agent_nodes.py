"""Tests for runtime/multi_agent/nodes.py (Phase 2).

Per plan Work unit 3:
  - ToolNode dispatches via ToolPipeline and emits a Message (Phase 1)
  - LLMNode.run() calls the harness LLM, increments state.llm_used, emits
    1 result-message; raises NotImplementedError if state.llm_provider
    is None (Phase 1 wiring contract preserved).
  - SubplanNode still raises NotImplementedError (Phase 4 stub).
  - ConsultNode.run() routes through ToolPipeline with consult_subagent
    as the executor; emits 1 result-message; stores answer in
    state.agent_outputs[self.id] via consult_subagent.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from tradingagents.agent_harness.llm.base import (
    LLMProvider,
    LLMResponse,
)
from tradingagents.agent_harness.runtime.multi_agent.graph import GraphSpec, NodeKind
from tradingagents.agent_harness.runtime.multi_agent.nodes import (
    ConsultNode,
    LLMNode,
    SubplanNode,
    ToolNode,
)
from tradingagents.agent_harness.runtime.multi_agent.state import (
    FieldRef,
    GraphState,
    TypedResult,
)


# Repo convention (no pytest-asyncio): sync wrapper that drives asyncio.run.
# The plan literally wrote ``async def _run(coro): return await coro`` but
# calling that from a sync ``def test_*`` returns an un-awaited coroutine —
# the inner pipeline / NotImplementedError never runs. Fix: use asyncio.run.
def _run(coro):
    return asyncio.run(coro)


class _FakeLLMProvider(LLMProvider):
    """Captures every complete() call. Returns a fixed answer."""

    name = "fake"

    def __init__(self, content: str = "stubbed-answer") -> None:
        self._content = content
        self.calls: list[list] = []

    def complete(self, messages, *, temperature=0.0, max_tokens=None, stop=None):
        self.calls.append(list(messages))
        return LLMResponse(content=self._content, provider="fake", model="fake")


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


def test_subplan_node_runs_sub_graph_and_writes_results_to_parent(monkeypatch):
    """Phase 4 WU1: SubplanNode executes self.sub_graph in a forked state,
    parent state preserved except for state.agent_outputs[self.id]."""
    import tradingagents.agent_harness.runtime.multi_agent.nodes as nodes_mod

    captured = {}

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            captured["tool_name"] = tool_name
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    inner = ToolNode(id="inner_a", agent_id="data_agent",
                     tool_name="get_quote", raw_args={"symbol": "X"})
    inner_spec = GraphSpec(
        nodes={"inner_a": inner}, edges=[], entry="inner_a", exit="inner_a",
    )
    outer = SubplanNode(id="sub", agent_id="data_agent",
                        sub_graph=inner_spec)
    state = GraphState(run_id="r", turn_id="t", intent="x")

    out = _run(outer.run(state, inbox=[]))

    # SubplanNode returns 1 result-message to next consumer
    assert len(out) == 1
    msg = out[0]
    assert msg.sender == "sub"
    # data shape mirrors ConsultNode pattern
    assert msg.payload.data["ok"] is True
    assert "sub_results" in msg.payload.data

    # Parent state.agent_outputs[self.id] populated
    assert "sub" in state.agent_outputs


def test_subplan_node_refuses_when_subplan_depth_at_max():
    """Phase 4 WU1: refuses with TypedResult(ok=False) when state.subplan_depth
    is already at state.subplan_max_depth (depth guard)."""
    inner = ToolNode(id="inner_a", agent_id="data_agent",
                     tool_name="get_quote", raw_args={"symbol": "X"})
    inner_spec = GraphSpec(
        nodes={"inner_a": inner}, edges=[], entry="inner_a", exit="inner_a",
    )
    outer = SubplanNode(id="sub", agent_id="data_agent",
                        sub_graph=inner_spec)

    state = GraphState(
        run_id="r", turn_id="t", intent="x",
        subplan_depth=3, subplan_max_depth=3,
    )

    out = _run(outer.run(state, inbox=[]))

    # Refusal result-message
    assert len(out) == 1
    typed = out[0].payload
    assert typed.data["ok"] is False
    assert typed.data["error"] == "SubplanDepthExceeded"
    assert typed.data["subplan_depth"] == 3
    assert typed.data["max_depth"] == 3
    assert typed.meta.get("refused") is True

    # Parent state.agent_outputs[self.id] NOT written on refusal
    assert "sub" not in state.agent_outputs


def test_subplan_node_respects_configurable_subplan_max_depth(monkeypatch):
    """Phase 4 WU1: subplan_max_depth is configurable on GraphState."""
    import tradingagents.agent_harness.runtime.multi_agent.nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    inner = ToolNode(id="inner_a", agent_id="data_agent",
                     tool_name="get_quote", raw_args={"symbol": "X"})
    inner_spec = GraphSpec(
        nodes={"inner_a": inner}, edges=[], entry="inner_a", exit="inner_a",
    )
    outer = SubplanNode(id="sub", agent_id="data_agent",
                        sub_graph=inner_spec)

    # depth=2, max=2 → refuse
    state_refuse = GraphState(
        run_id="r", turn_id="t", intent="x",
        subplan_depth=2, subplan_max_depth=2,
    )
    out_refuse = _run(outer.run(state_refuse, inbox=[]))
    assert out_refuse[0].payload.data["ok"] is False

    # depth=1, max=3 → proceed (write agent_outputs)
    state_proceed = GraphState(
        run_id="r", turn_id="t", intent="x",
        subplan_depth=1, subplan_max_depth=3,
    )
    out_proceed = _run(outer.run(state_proceed, inbox=[]))
    assert out_proceed[0].payload.data["ok"] is True
    assert "sub" in state_proceed.agent_outputs


# ────────────────────────────────────────────────────────────────────────
# LLMNode — Phase 2 Work unit 3
# ────────────────────────────────────────────────────────────────────────

def test_llm_node_happy_path_emits_message_and_increments_budget():
    provider = _FakeLLMProvider(content="my-answer")
    state = GraphState(run_id="r", turn_id="t", intent="x",
                       llm_provider=provider)
    node = LLMNode(id="a", agent_id="data_agent",
                   system_prompt="You are a helpful agent.")

    out = _run(node.run(state, inbox=[]))

    assert len(out) == 1
    msg = out[0]
    assert msg.sender == "a"
    assert msg.payload.data["answer"] == "my-answer"
    assert msg.payload.data["llm_used"] == 1
    assert msg.payload.meta["source_agent"] == "data_agent"
    assert state.llm_used == 1
    # LLM was called once with system + user prompt
    assert len(provider.calls) == 1
    msgs = provider.calls[0]
    assert len(msgs) == 2
    assert msgs[0].role == "system"
    assert "helpful agent" in msgs[0].content
    assert msgs[1].role == "user"


def test_llm_node_no_provider_raises_not_implemented_and_skips_budget():
    """Phase 2 contract: missing state.llm_provider → NotImplementedError.
    Executor swallows (Phase 1 wiring tests rely on this) and llm_used
    is NOT incremented because the raise happens BEFORE the increment."""
    state = GraphState(run_id="r", turn_id="t", intent="x")
    assert state.llm_provider is None  # default
    node = LLMNode(id="b", agent_id="data_agent", system_prompt="...")

    with pytest.raises(NotImplementedError):
        _run(node.run(state, inbox=[]))
    assert state.llm_used == 0


def test_llm_node_extracts_question_from_inbox_string():
    provider = _FakeLLMProvider(content="answer")
    state = GraphState(run_id="r", turn_id="t", intent="x",
                       llm_provider=provider)
    node = LLMNode(id="c", agent_id="data_agent", system_prompt="...")

    # Pre-populate an inbox with a string-typed question.
    inbox = [
        _msg("upstream", "c", "What is the price trend for 600036.SS?"),
    ]
    out = _run(node.run(state, inbox=inbox))

    # Question was extracted into the user prompt.
    user_prompt = provider.calls[0][1].content
    assert "What is the price trend for 600036.SS?" in user_prompt
    assert len(out) == 1
    assert out[0].payload.data["llm_used"] == 1


# ────────────────────────────────────────────────────────────────────────
# ConsultNode — Phase 2 Work unit 3
# ────────────────────────────────────────────────────────────────────────

def test_consult_node_happy_path_stores_and_emits(monkeypatch):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    provider = _FakeLLMProvider(content="answer-from-alpha")
    state = GraphState(run_id="r", turn_id="t", intent="x",
                       llm_provider=provider)

    captured = {}

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            captured["tool_name"] = tool_name
            captured["args"] = args
            captured["tool_context"] = tool_context
            # Drive the executor so consult_subagent actually runs.
            result_dict = await executor(args, tool_context)
            return MagicMock(ok=True, result=result_dict, error=None)

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    node = ConsultNode(
        id="c", agent_id="data_agent",
        target_agent="alpha_agent", question="why?",
    )
    out = _run(node.run(state, inbox=[]))

    # 1. One emitted message
    assert len(out) == 1
    msg = out[0]
    assert msg.sender == "c"
    assert msg.payload.data["ok"] is True
    assert msg.payload.data["answer"] == "answer-from-alpha"

    # 2. consult_subagent stored answer in state.agent_outputs[self.id]
    assert "c" in state.agent_outputs
    assert state.agent_outputs["c"].data == "answer-from-alpha"
    assert state.agent_outputs["c"].meta["source_agent"] == "alpha_agent"

    # 3. consultation_used incremented by consult_subagent
    assert state.consultation_used == 1

    # 4. ToolPipeline was invoked with the right tool name + context
    assert captured["tool_name"] == "consult_subagent"
    assert captured["tool_context"].extra["graph_state"] is state
    assert captured["tool_context"].extra["graph_node_id"] == "c"
    assert captured["tool_context"].extra["llm_provider"] is provider

    # 5. Args built correctly from ConsultNode fields
    assert captured["args"].target_agent == "alpha_agent"
    assert captured["args"].question == "why?"
    assert captured["args"].context_refs == []  # no inputs
    assert captured["args"].max_tokens == 512


def test_consult_node_serializes_inputs_to_context_refs(monkeypatch):
    """Plan §1: context_refs = [ref.agent + ref.field for ref in self.inputs]."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    provider = _FakeLLMProvider(content="answer")
    state = GraphState(run_id="r", turn_id="t", intent="x",
                       llm_provider=provider)

    captured = {}

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            captured["args"] = args
            return MagicMock(ok=True, result={"answer": "x"}, error=None)

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    node = ConsultNode(
        id="c", agent_id="data_agent",
        target_agent="alpha_agent", question="?",
        inputs=[
            FieldRef(agent="data", field="q.symbol"),
            FieldRef(agent="data", field="q.meta.isin"),
        ],
    )
    _run(node.run(state, inbox=[]))

    assert captured["args"].context_refs == ["data.q.symbol", "data.q.meta.isin"]


def test_consult_node_pipeline_error_emits_ok_false_message(monkeypatch):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=False, result=None,
                             error="consult rate exceeded")

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    provider = _FakeLLMProvider(content="should-not-be-called")
    state = GraphState(run_id="r", turn_id="t", intent="x",
                       llm_provider=provider)
    node = ConsultNode(id="d", agent_id="data_agent",
                       target_agent="alpha_agent", question="x")

    out = _run(node.run(state, inbox=[]))

    assert len(out) == 1
    assert out[0].payload.data["ok"] is False
    assert out[0].payload.data["error"] == "consult rate exceeded"
    # consult_subagent never ran → no state mutation
    assert state.consultation_used == 0
    assert "d" not in state.agent_outputs
    # LLM provider never called
    assert provider.calls == []


def test_consult_node_consult_budget_exceeded_propagates_through_pipeline(monkeypatch):
    """Phase 2 Work unit 4 — verify the executor raises ConsultBudgetExceeded
    when consultation_used is already at the cap; the pipeline catches the
    exception and ConsultNode.run() emits ok=False."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            # Drive the executor — it raises ConsultBudgetExceeded.
            return await _real_pipeline_run(tool_name, args, tool_context, executor)

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    provider = _FakeLLMProvider(content="should-not-be-called")
    state = GraphState(run_id="r", turn_id="t", intent="x",
                       llm_provider=provider, budget_limit=5,
                       consultation_rate_limit=0.5)
    # Pre-fill to the cap: ceil(0.5 * 5) = 2
    state.consultation_used = 2

    node = ConsultNode(id="e", agent_id="data_agent",
                       target_agent="alpha_agent", question="x")
    out = _run(node.run(state, inbox=[]))

    assert len(out) == 1
    assert out[0].payload.data["ok"] is False
    assert "consultation rate limit" in out[0].payload.data["error"]
    assert state.consultation_used == 2  # not incremented (refused)


async def _real_pipeline_run(tool_name, args, tool_context, executor):
    """Helper: invokes executor directly (no real pipeline). Matches the
    behaviour a real pipeline would have: catches exceptions and returns
    a MagicMock(ok=False, error=str(exc)) when the executor raises."""
    from tradingagents.agent_harness.tools.pipeline import ToolPipeline
    # Use the real pipeline so it captures the exception via its try/except.
    pipeline = ToolPipeline()
    return await pipeline.run(
        tool_name=tool_name, args=args, tool_context=tool_context,
        executor=executor,
    )


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

def _msg(sender: str, receiver: str, data: str):
    """Build a simple inbound message for inbox tests."""
    return _make_message(sender, receiver, data)


def _make_message(sender: str, receiver: str, data: str):
    from tradingagents.agent_harness.runtime.multi_agent.state import Message
    return Message(
        sender=sender, receiver=receiver,
        payload=TypedResult(schema=str, data=data, meta={}),
    )



# ────────────────────────────────────────────────────────────────────────
# ToolNode — Phase 3 Work unit 4 ($ref resolution at runtime, spec §4.5)
# ────────────────────────────────────────────────────────────────────────

def test_tool_node_resolves_simple_dollar_ref_to_actual_value(monkeypatch):
    """$data.quote.symbol resolves to state.agent_outputs['data'].data['quote']['symbol']."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    captured = {}

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            captured["args"] = args
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x")
    state.agent_outputs["data"] = TypedResult(
        schema=dict,
        data={"quote": {"symbol": "600036.SS"}, "x": 40.5},
        meta={},
    )
    node = ToolNode(
        id="a", agent_id="data_agent",
        tool_name="get_quote",
        raw_args={"symbol": "$data.quote.symbol"},
    )
    _run(node.run(state, inbox=[]))
    assert captured["args"] == {"symbol": "600036.SS"}


def test_tool_node_resolves_arithmetic_expression(monkeypatch):
    """$data.x * 1.5 resolves to the arithmetic result (Phase 1 resolver)."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    captured = {}

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            captured["args"] = args
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x")
    state.agent_outputs["data"] = TypedResult(
        schema=dict, data={"x": 40.5}, meta={},
    )
    node = ToolNode(
        id="a", agent_id="data_agent",
        tool_name="compute_alpha_factors",
        raw_args={"weight": "$data.x * 1.5"},
    )
    _run(node.run(state, inbox=[]))
    assert captured["args"] == {"weight": 60.75}


def test_tool_node_unresolved_ref_falls_back_to_literal(monkeypatch):
    """$unknown.y with no agent_outputs['unknown'] → kept as literal string
    (spec §4.5 graceful degradation — preserves LLM intent)."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    captured = {}

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            captured["args"] = args
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x")
    # Intentionally NOT populating state.agent_outputs["unknown"].
    node = ToolNode(
        id="a", agent_id="data_agent",
        tool_name="get_quote",
        raw_args={"bad": "$unknown.y"},
    )
    _run(node.run(state, inbox=[]))
    assert captured["args"] == {"bad": "$unknown.y"}


def test_tool_node_mixed_args_resolves_strings_passes_other_types(monkeypatch):
    """Mixed dict: string $refs resolve; non-string types pass through unchanged."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    captured = {}

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            captured["args"] = args
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x")
    state.agent_outputs["data"] = TypedResult(
        schema=dict, data={"quote": {"symbol": "600036.SS"}}, meta={},
    )
    node = ToolNode(
        id="a", agent_id="data_agent",
        tool_name="get_quote",
        raw_args={
            "symbol": "$data.quote.symbol",
            "limit": 10,
            "tags": ["a", "b"],
            "flag": True,
            "unused": None,
        },
    )
    _run(node.run(state, inbox=[]))
    assert captured["args"] == {
        "symbol": "600036.SS",   # resolved
        "limit": 10,             # int, passthrough
        "tags": ["a", "b"],      # list, passthrough
        "flag": True,            # bool, passthrough
        "unused": None,          # None, passthrough
    }
