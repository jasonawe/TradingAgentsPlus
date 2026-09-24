"""Tests for consult_subagent tool wrapper (Phase 2 — real LLM call).

Per spec §4.10:
- Build context from context_refs (resolved via state.agent_outputs)
- Call target_agent's LLM with system_prompt + context + question
- Return answer (no tools in consult mode)
- 1 LLM call counted against state.consultation_used
- Refuse with ConsultBudgetExceeded when over consultation_rate_limit * budget_limit
- Store answer in state.agent_outputs[node_id]
"""
import asyncio
import inspect
import pytest

from tradingagents.agent_harness.llm.base import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
)
from tradingagents.agent_harness.runtime.multi_agent.state import (
    GraphState,
    TypedResult,
)
from tradingagents.agent_harness.tools.context import ToolContext
from tradingagents.agent_harness.tools.builtin_consult import (
    ConsultBudgetExceeded,
    ConsultSubagentArgs,
    consult_subagent,
)


def _run(coro):
    """Repo convention (no pytest-asyncio): drive coroutines via asyncio.run."""
    return asyncio.run(coro)


class _FakeProvider(LLMProvider):
    """LLMProvider stub returning a fixed string. Records the messages
    it was called with so tests can assert on the prompt composition."""

    name = "fake"

    def __init__(self, content: str = "stubbed-answer") -> None:
        self._content = content
        self.calls: list[tuple[list[ChatMessage], dict]] = []

    def complete(self, messages, *, temperature=0.0, max_tokens=None, stop=None):
        self.calls.append((list(messages), {"max_tokens": max_tokens}))
        return LLMResponse(content=self._content, provider="fake", model="fake")


def test_consult_args_validation():
    args = ConsultSubagentArgs(target_agent="data_agent", question="why?")
    assert args.target_agent == "data_agent"
    assert args.max_tokens == 512
    with pytest.raises(Exception):
        ConsultSubagentArgs(target_agent="", question="x")


def test_consult_subagent_signature_uses_context_with_default_none():
    sig = inspect.signature(consult_subagent)
    params = sig.parameters
    assert "context" in params, (
        f"FunctionTool.invoke only auto-injects the tool context when the "
        f"param name is 'context' (see tools/base.py); got {list(params)!r}"
    )
    assert params["context"].default is None, (
        "context must default to None to match the convention in tools/builtin.py"
    )


def test_consult_subagent_happy_path_increments_and_stores_answer():
    """§4.10: happy path returns non-empty str; consultation_used increments;
    state.agent_outputs[node_id] holds the answer."""
    state = GraphState(run_id="r", turn_id="t", intent="x",
                       budget_limit=5, consultation_rate_limit=0.5)
    # Pre-populate an upstream output so context_refs has something to resolve.
    state.agent_outputs["data.q"] = TypedResult(
        schema=dict,
        data={"symbol": "600036.SS", "price": 40.5},
        meta={},
    )
    provider = _FakeProvider(content="ANSWER-from-alpha-agent")
    ctx = ToolContext(
        session_id="s", intent="x",
        extra={
            "graph_state": state,
            "graph_node_id": "consult_node_1",
            "llm_provider": provider,
        },
    )
    args = ConsultSubagentArgs(
        target_agent="alpha_agent",
        question="What's the price trend?",
        context_refs=["data.q"],
        max_tokens=256,
    )

    out = _run(consult_subagent(args, context=ctx))

    # 1. Returns dict with non-empty answer
    assert isinstance(out, dict)
    assert out["answer"] == "ANSWER-from-alpha-agent"
    assert isinstance(out["answer"], str) and out["answer"]
    # 2. consultation_used incremented exactly once
    assert state.consultation_used == 1
    # 3. answer stored under graph_node_id
    assert "consult_node_1" in state.agent_outputs
    stored = state.agent_outputs["consult_node_1"]
    assert stored.data == "ANSWER-from-alpha-agent"
    assert stored.meta.get("source_agent") == "alpha_agent"
    # 4. LLM was called once with composed system+user prompt
    assert len(provider.calls) == 1
    msgs, kw = provider.calls[0]
    assert len(msgs) == 2
    assert msgs[0].role == "system"
    assert "alpha_agent" in msgs[0].content
    assert msgs[1].role == "user"
    assert "What's the price trend?" in msgs[1].content
    # 5. context_refs resolved into the prompt
    assert "600036.SS" in msgs[1].content
    # 6. max_tokens passed through
    assert kw["max_tokens"] == 256


def test_consult_subagent_budget_exceeded_raises_and_does_not_call_llm():
    """§4.10: when consultation_used >= ceil(rate_limit * budget_limit),
    raise ConsultBudgetExceeded BEFORE calling the LLM."""
    state = GraphState(run_id="r", turn_id="t", intent="x",
                       budget_limit=5, consultation_rate_limit=0.5)
    # Pre-fill to the cap: ceil(0.5 * 5) = 2
    state.consultation_used = 2
    provider = _FakeProvider(content="should-not-be-called")
    ctx = ToolContext(
        session_id="s", intent="x",
        extra={
            "graph_state": state,
            "graph_node_id": "consult_node_2",
            "llm_provider": provider,
        },
    )
    args = ConsultSubagentArgs(target_agent="alpha_agent", question="x")

    with pytest.raises(ConsultBudgetExceeded):
        _run(consult_subagent(args, context=ctx))

    # LLM NOT called (budget guard fires before invocation)
    assert provider.calls == []
    # consultation_used NOT incremented
    assert state.consultation_used == 2


def test_consult_subagent_missing_provider_raises():
    """When context.extra has no llm_provider, refuse (read-only mode
    has nothing to consult)."""
    state = GraphState(run_id="r", turn_id="t", intent="x")
    ctx = ToolContext(
        session_id="s", intent="x",
        extra={"graph_state": state, "graph_node_id": "n"},
    )
    args = ConsultSubagentArgs(target_agent="alpha_agent", question="x")

    with pytest.raises(ConsultBudgetExceeded):
        _run(consult_subagent(args, context=ctx))


def test_consult_subagent_no_state_runs_but_does_not_store():
    """Phase 2 consult may be called outside the multi_agent executor
    (e.g. unit testing or future direct invocation paths). Without a
    state in context.extra, the call still succeeds but consultation_used
    is not incremented and the answer is not stored."""
    provider = _FakeProvider(content="direct-answer")
    ctx = ToolContext(
        session_id="s", intent="x",
        extra={"llm_provider": provider},  # no graph_state / graph_node_id
    )
    args = ConsultSubagentArgs(target_agent="alpha_agent", question="x")

    out = _run(consult_subagent(args, context=ctx))

    assert out["answer"] == "direct-answer"
    assert out["consultation_used"] == 0
    # No state to store into; just confirm the call didn't crash.
    assert len(provider.calls) == 1


# ----------------------------------------------------------------------
# §0.4.35 phase 2 — Work unit 2: registration + _TOOL_TO_AGENT mapping
# ----------------------------------------------------------------------

def test_consult_subagent_registered_in_tool_registry():
    """After Work unit 2, install_builtin_tools registers consult_subagent."""
    from tradingagents.agent_harness.tools.builtin import install_builtin_tools
    from tradingagents.agent_harness.tools.registry import ToolRegistry
    from tradingagents.agent_harness.tools.builtin_consult import (
        consult_subagent, ConsultSubagentArgs,
    )

    reg = ToolRegistry()
    install_builtin_tools(reg)

    tool = reg.get("consult_subagent")
    assert tool is not None, "consult_subagent must be registered"
    assert tool.name == "consult_subagent"
    assert tool.schema.args_schema is ConsultSubagentArgs
    # tool wraps the same callable the module exports
    assert tool._func is consult_subagent


def test_consult_subagent_in_TOOL_TO_AGENT():
    """consult_subagent routes to the synthesizer agent (spec §4.10)."""
    from tradingagents.agent_harness.core.orchestrator import _TOOL_TO_AGENT

    assert "consult_subagent" in _TOOL_TO_AGENT
    assert _TOOL_TO_AGENT["consult_subagent"] == "synthesizer"
