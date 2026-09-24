"""Phase 1 node implementations.

Only ``ToolNode`` is wired for execution (via ToolPipeline). The other three
raise ``NotImplementedError`` until their respective phases land.
"""
from __future__ import annotations
import time
from typing import Any, Optional

from .graph import NodeKind
from .state import FieldRef, GraphState, Message, TypedResult


def _default_pipeline():
    """Lazy import to avoid cycles and to let tests monkeypatch."""
    from tradingagents.agent_harness.tools.pipeline import ToolPipeline
    return ToolPipeline()


class ToolNode:
    kind = NodeKind.TOOL

    def __init__(self, id: str, agent_id: str, tool_name: str,
                 raw_args: dict[str, Any],
                 inputs: Optional[list[FieldRef]] = None,
                 outputs: Optional[list[FieldRef]] = None) -> None:
        self.id = id
        self.agent_id = agent_id
        self.tool_name = tool_name
        self.raw_args = raw_args
        self.inputs: list[FieldRef] = list(inputs or [])
        self.outputs: list[FieldRef] = list(outputs or [])

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        from tradingagents.agent_harness.tools.context import ToolContext
        ctx = ToolContext(session_id=state.run_id, intent=state.intent)
        pipeline = _default_pipeline()

        async def _noop_executor(args, context):
            return {"phase1_stub": True, "tool": self.tool_name, "args": args}
        pipe_res = await pipeline.run(
            tool_name=self.tool_name,
            args=dict(self.raw_args or {}),
            tool_context=ctx,
            executor=_noop_executor,
        )
        result = pipe_res.result if pipe_res.ok else {"error": pipe_res.error}
        # Phase 4 verifier reads source_ts; populate it now per spec §7.
        typed = TypedResult(
            schema=dict, data=result,
            meta={"tool": self.tool_name, "source_ts": time.monotonic(),
                  "source_agent": self.agent_id},
        )
        return [Message(sender=self.id, receiver="*", payload=typed)]


class LLMNode:
    kind = NodeKind.LLM

    def __init__(self, id: str, agent_id: str, system_prompt: str,
                 inputs: Optional[list[FieldRef]] = None,
                 outputs: Optional[list[FieldRef]] = None) -> None:
        self.id = id
        self.agent_id = agent_id
        self.system_prompt = system_prompt
        self.inputs: list[FieldRef] = list(inputs or [])
        self.outputs: list[FieldRef] = list(outputs or [])

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        raise NotImplementedError("LLMNode lands in Phase 2")


class SubplanNode:
    kind = NodeKind.SUBPLAN

    def __init__(self, id: str, agent_id: str, sub_graph,
                 inputs: Optional[list[FieldRef]] = None,
                 outputs: Optional[list[FieldRef]] = None) -> None:
        self.id = id
        self.agent_id = agent_id
        self.sub_graph = sub_graph
        self.inputs: list[list[FieldRef]] = list(inputs or [])
        self.outputs: list[FieldRef] = list(outputs or [])

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        raise NotImplementedError("SubplanNode lands in Phase 4")


class ConsultNode:
    kind = NodeKind.CONSULT

    def __init__(self, id: str, agent_id: str, target_agent: str,
                 question: str,
                 inputs: Optional[list[FieldRef]] = None,
                 outputs: Optional[list[FieldRef]] = None) -> None:
        self.id = id
        self.agent_id = agent_id
        self.target_agent = target_agent
        self.question = question
        self.inputs: list[FieldRef] = list(inputs or [])
        self.outputs: list[FieldRef] = list(outputs or [])

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        raise NotImplementedError("ConsultNode lands in Phase 2")
