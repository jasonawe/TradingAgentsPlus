"""Stub for `consult_subagent` — Phase 2 wires registration + real LLM call.

Phase 1 surfaces the contract only. Registration happens in Phase 2 via
``@tool_registry.register(...)`` on the per-harness ``ToolRegistry``.

Rev.5 contract: the ``context`` parameter uses the standard ToolContext type
(defaults to None), matching every other built-in tool in ``tools/builtin.py``.
Renaming or retyping this in Phase 2 will break callers, so the Phase 1 stub
locks the contract.
"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field

from tradingagents.agent_harness.tools.context import ToolContext

class ConsultSubagentArgs(BaseModel):
    target_agent: str = Field(min_length=1)
    question: str = Field(min_length=1)
    context_refs: list[str] = Field(default_factory=list)
    max_tokens: int = Field(default=512, ge=64, le=2048)

async def consult_subagent(
    args: ConsultSubagentArgs,
    context: ToolContext | None = None,
) -> dict[str, Any]:
    """Phase 1: surface only. Real LLM call lands in Phase 2."""
    raise NotImplementedError(
        "consult_subagent lands in §0.4.35 phase 2 "
        f"(asked target={args.target_agent!r})"
    )
