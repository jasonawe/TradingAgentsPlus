import asyncio
import inspect
import pytest
from tradingagents.agent_harness.tools.context import ToolContext
from tradingagents.agent_harness.tools.builtin_consult import (
    ConsultSubagentArgs, consult_subagent,
)

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

def test_consult_subagent_stub_raises():
    args = ConsultSubagentArgs(target_agent="data_agent", question="hi")
    ctx = ToolContext(session_id="r", intent="x")
    with pytest.raises(NotImplementedError):
        asyncio.run(consult_subagent(args, context=ctx))
