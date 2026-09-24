"""Phase 2 node implementations.

Phase 1 surfaced the API surface with ``NotImplementedError`` stubs.
Phase 2 wires real behaviour:

- ``ToolNode``: dispatches via ``ToolPipeline`` (Phase 1).
- ``LLMNode``: builds a composed prompt (system + inbox context +
  extracted question), calls the LLM provider on
  ``state.llm_provider``, increments ``state.llm_used`` after a
  successful call, emits a result-message. Raises
  ``NotImplementedError`` when ``state.llm_provider`` is ``None`` —
  the executor swallows that exception (Phase 1 wiring test
  contract preserved).
- ``SubplanNode``: still a stub — Phase 4 wires ``fork()`` +
  nested ``GraphExecutor.run()``.
- ``ConsultNode``: builds ``ConsultSubagentArgs`` from
  ``self.inputs`` + ``self.target_agent`` + ``self.question``,
  invokes ``consult_subagent`` through ``ToolPipeline`` (spec §4.10
  requires consult to flow through the tool layer, not a direct
  LLM call). The pipeline's executor IS the ``consult_subagent``
  coroutine, so budget guards + provider wiring + answer storage
  all live in one place. Emits a result-message that wraps the
  consult payload (with ``ok=False`` when the pipeline reports
  failure).
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
        """Spec §4.6: build prompt + call harness LLM + emit result.

        Raises ``NotImplementedError`` if ``state.llm_provider`` is not
        wired — the GraphExecutor's ``except NotImplementedError``
        handler catches this, logs a warning, and continues without
        bumping ``state.llm_used``. This preserves the Phase 1 wiring
        contract (test_executor_swallows_not_implemented_for_llm_node)
        where a graph containing an LLMNode with no provider simply
        no-ops.
        """
        provider = state.llm_provider
        if provider is None:
            raise NotImplementedError(
                f"LLMNode {self.id!r} requires state.llm_provider "
                f"(orchestrator wiring is responsible for setting it "
                f"before GraphExecutor.run())"
            )

        # Compose inbox context. TypedResult.data is the canonical place
        # for the message body — fall back to its string repr otherwise.
        ctx_lines = []
        for m in inbox:
            data = m.payload.data if isinstance(m.payload.data, str) \
                else str(m.payload.data)
            ctx_lines.append(f"[{m.sender} → {m.receiver}]: {data}")
        inbox_str = "\n".join(ctx_lines) if ctx_lines else "(no upstream messages)"

        # Extract a question from the first inbox message (string-typed
        # data wins). Fall back to a role-based prompt when inbox is empty
        # — entry-node LLMNodes start with no upstream messages.
        if inbox and isinstance(inbox[0].payload.data, str):
            question = inbox[0].payload.data
        else:
            question = (
                f"Provide your analysis for the {self.agent_id} agent."
            )

        system_prompt = self.system_prompt or f"You are the {self.agent_id} agent."
        user_prompt = (
            f"Context:\n{inbox_str}\n\n"
            f"Question: {question}"
        )

        from tradingagents.agent_harness.llm.base import ChatMessage
        response = provider.complete(
            messages=[
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_prompt),
            ],
        )

        # Budget accounting AFTER the call — so a failed call (raised)
        # doesn't consume budget. The executor's ``except Exception``
        # path catches failures and skips the increment too.
        state.llm_used += 1

        typed = TypedResult(
            schema=str,
            data={"answer": response.content, "llm_used": state.llm_used},
            meta={
                "source_ts": time.monotonic(),
                "source_agent": self.agent_id,
            },
        )
        return [Message(sender=self.id, receiver=None, payload=typed)]


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
        """Spec §4.10 + §4.6: build ConsultSubagentArgs + invoke via
        ToolPipeline (consult is a tool, not a direct LLM call).

        The pipeline's ``executor`` is the ``consult_subagent`` coroutine
        — it owns budget enforcement, context-refs resolution, and
        ``state.agent_outputs[self.id]`` storage (Phase 2 Work unit 1).
        We wrap the result in a TypedResult so downstream nodes can
        ``$ref`` it via ``state.agent_outputs[self.id].data``.
        """
        from tradingagents.agent_harness.tools.context import ToolContext
        from tradingagents.agent_harness.tools.builtin_consult import (
            ConsultSubagentArgs,
            consult_subagent,
        )

        # Serialize FieldRef inputs to "$agent.$field" strings.
        context_refs = [
            f"{ref.agent}.{ref.field}" for ref in self.inputs
        ]
        args = ConsultSubagentArgs(
            target_agent=self.target_agent,
            question=self.question,
            context_refs=context_refs,
            max_tokens=512,
        )

        ctx = ToolContext(
            session_id=state.run_id,
            user_id="phase2-multi-agent",
            intent=state.intent,
            extra={
                "graph_state": state,
                "graph_node_id": self.id,
                "llm_provider": state.llm_provider,
            },
        )

        pipeline = _default_pipeline()

        async def _executor(a, c):
            return await consult_subagent(a, c)

        pipe_res = await pipeline.run(
            tool_name="consult_subagent",
            args=args,
            tool_context=ctx,
            executor=_executor,
        )

        if pipe_res.ok:
            payload = pipe_res.result or {}
            typed = TypedResult(
                schema=dict,
                data={"ok": True, **payload},
                meta={
                    "source_ts": time.monotonic(),
                    "source_agent": self.agent_id,
                    "source_tool": "consult_subagent",
                },
            )
        else:
            typed = TypedResult(
                schema=dict,
                data={"ok": False, "error": pipe_res.error},
                meta={
                    "source_ts": time.monotonic(),
                    "source_agent": self.agent_id,
                    "source_tool": "consult_subagent",
                },
            )

        return [Message(sender=self.id, receiver=None, payload=typed)]
