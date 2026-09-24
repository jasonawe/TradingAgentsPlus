"""Phase 2 — ``consult_subagent`` tool wrapper (real LLM call).

Per spec §4.10: build context from ``context_refs`` (resolved via
``state.agent_outputs``), call the target_agent's LLM with a
composed (system_prompt + context + question) prompt, return the
answer, count against ``state.consultation_used``, and store the
answer under ``state.agent_outputs[graph_node_id]`` so the calling
node can ``$ref`` it in subsequent steps.

Read-only mode: ``consult_subagent`` may NOT invoke tools (no nested
consult, no tool use) — only the LLM is called.

Budget: refuses with :class:`ConsultBudgetExceeded` when
``consultation_used >= consultation_rate_limit * budget_limit``.
"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field

from tradingagents.agent_harness.tools.context import ToolContext


class ConsultBudgetExceeded(Exception):
    """Raised when consultation rate limit or wiring is violated.

    Two trigger paths:
    - ``state.consultation_used >= ceil(rate_limit * budget_limit)``
    - missing ``llm_provider`` in ``context.extra`` (consult has
      no LLM to call; read-only mode refuses to silently no-op)
    """


class ConsultSubagentArgs(BaseModel):
    target_agent: str = Field(min_length=1)
    question: str = Field(min_length=1)
    context_refs: list[str] = Field(default_factory=list)
    max_tokens: int = Field(default=512, ge=64, le=2048)


# Module-level system prompt template. Phase 2 uses a generic per-agent
# prompt; Phase 3 may consult ``agents/registry.py`` for richer per-agent
# system prompts.
_SYSTEM_PROMPT_TEMPLATE = (
    "You are the {target_agent} sub-agent. Answer the user's question "
    "concisely and directly. Do not invoke any tools — return a direct "
    "answer only."
)


async def consult_subagent(
    args: ConsultSubagentArgs,
    context: ToolContext | None = None,
) -> dict[str, Any]:
    """Spec §4.10 inter-agent dialogue.

    Returns a dict ``{"answer": str, "consultation_used": int,
    "budget_remaining": int|None}``. Raises
    :class:`ConsultBudgetExceeded` on budget exhaustion or missing
    LLM provider wiring.
    """
    extra = (context.extra or {}) if context else {}
    state = extra.get("graph_state")
    node_id = extra.get("graph_node_id")
    provider = extra.get("llm_provider")

    cap = (
        int(state.consultation_rate_limit * state.budget_limit)
        if state is not None else 0
    )

    # 1. Budget guard — fires BEFORE incrementing or calling the LLM.
    #    We refuse before counting so a budget_exceeded call does not
    #    consume the slot it just failed to acquire.
    if state is not None and state.consultation_used >= cap:
        raise ConsultBudgetExceeded(
            f"consultation rate limit exceeded: "
            f"{state.consultation_used}/{cap} "
            f"(consultation_rate_limit={state.consultation_rate_limit}, "
            f"budget_limit={state.budget_limit})"
        )

    # 2. Provider wiring guard — no provider = nothing to consult.
    #    Treat as budget violation so the executor's existing
    #    ``except Exception`` fall-through path handles it.
    if provider is None:
        raise ConsultBudgetExceeded(
            "consult_subagent requires llm_provider in context.extra "
            "(wiring is the orchestrator's responsibility)"
        )

    # 3. Increment the budget counter — after the guards above so a
    #    refused call does not bump the counter.
    if state is not None:
        state.consultation_used += 1

    # 4. Resolve context_refs against state.agent_outputs.
    ctx_lines: list[str] = []
    if state is not None:
        for ref in (args.context_refs or []):
            out = state.agent_outputs.get(ref)
            if out is None:
                ctx_lines.append(f"[{ref}]: (unresolved)")
            else:
                ctx_lines.append(f"[{ref}]: {getattr(out, 'data', out)}")
    ctx_str = "\n".join(ctx_lines) if ctx_lines else "(no context refs)"

    # 5. Compose prompt + invoke LLM (read-only — no tool binding).
    from tradingagents.agent_harness.llm.base import ChatMessage
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(target_agent=args.target_agent)
    user_prompt = (
        f"Context from upstream agents:\n{ctx_str}\n\n"
        f"Question: {args.question}"
    )
    response = provider.complete(
        messages=[
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        ],
        max_tokens=args.max_tokens,
    )
    answer = response.content

    # 6. Store answer under node_id so callers can $ref it.
    if state is not None and node_id:
        from tradingagents.agent_harness.runtime.multi_agent.state import (
            TypedResult,
        )
        state.agent_outputs[node_id] = TypedResult(
            schema=str,
            data=answer,
            meta={
                "source_agent": args.target_agent,
                "source_tool": "consult_subagent",
            },
        )

    return {
        "answer": answer,
        "consultation_used": state.consultation_used if state is not None else 0,
        "budget_remaining": (
            cap - state.consultation_used
            if state is not None else None
        ),
    }
