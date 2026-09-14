"""VerifierAgent — L3 LLM-judge (v3 spec §4.1, D6).

Default behavior: structural + semantic checks (no LLM). When
``enable_l3=True`` is set on the orchestrator, subclasses override to
call an LLM and produce a judgement string.
"""
from __future__ import annotations

from .base import AgentContext, AgentInput, AgentResult, BaseAgent


class VerifierAgent(BaseAgent):
    name = "verifier"
    description = "L3 LLM-judge for tool_results and final answers (D6)."
    tools: list = []

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        tool_results = (input.context or {}).get("tool_results", [])
        errors = [r for r in tool_results if isinstance(r, dict) and r.get("error")]
        warnings = [r for r in tool_results if isinstance(r, dict) and r.get("warnings")]
        ok = len(errors) == 0
        reason_parts: list[str] = []
        if errors:
            reason_parts.append(f"{len(errors)} tool errors")
        if warnings:
            reason_parts.append(f"{len(warnings)} warnings")
        if not reason_parts:
            reason_parts.append("all checks passed")
        return AgentResult(
            success=ok,
            content=" | ".join(reason_parts),
            structured_data={"errors": errors, "warnings": warnings},
        )
