"""SynthesizerAgent — 回答合成 (v3 spec §4.1, P8 LLM integration).

When ``llm_factory`` is wired, the agent summarizes verified tool
results into a natural-language answer. Falls back to a structured
placeholder otherwise.
"""
from __future__ import annotations

import json as _json
import logging

from .base import AgentContext, AgentInput, AgentResult, BaseAgent

LOGGER = logging.getLogger(__name__)


class SynthesizerAgent(BaseAgent):
    name = "synthesizer"
    description = "Synthesize a natural-language answer from verified tool results."
    tools: list = []
    system_prompt = (
        "You are SynthesizerAgent. Given verified tool results and the "
        "user's original question, write a concise, accurate answer. "
        "Reply in the same language the user used."
    )

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        tool_results = (input.context or {}).get("tool_results", [])
        symbols = (input.context or {}).get("symbols", [])

        # LLM path.
        if tool_results and self._llm_available():
            prompt = (
                f"User message: {input.user_message}\n\n"
                f"Tool results: {_json.dumps(tool_results, ensure_ascii=False, default=str)[:6000]}\n\n"
                "Write a concise answer in the same language as the user message."
            )
            summary = self._llm_complete(prompt)
            if summary:
                return AgentResult(
                    success=True,
                    content=summary,
                    structured_data={"tool_count": len(tool_results), "symbols": symbols, "source": "llm"},
                    tool_results=tool_results,
                )

        # Stub fallback (preserves previous behavior for tests).
        content_parts = [f"processed {len(tool_results)} tool_results"]
        if symbols:
            content_parts.append(f"for {', '.join(symbols)}")
        return AgentResult(
            success=True,
            content=" — ".join(content_parts),
            structured_data={"tool_count": len(tool_results), "symbols": symbols},
        )


# ════════════════════════════════════════════════════════
# V2 — synthesize from verified refs only (Task 17)
# ════════════════════════════════════════════════════════


async def _synthesizer_run_v2(
    self, input: AgentInput, *, context: AgentContext
):
    """V2 entry: synthesize only from VerifiedEvidenceRef."""
    from .base import AgentReply

    ctx_data = input.context or {}
    refs = ctx_data.get("evidence_refs") or []
    objective = ctx_data.get("objective") or input.user_message or ""

    verified = [r for r in refs if r.get("verification_level")]
    unverified = [r for r in refs if not r.get("verification_level")]

    if unverified:
        # 拒绝 plain refs
        return AgentReply(
            success=False,
            content="",
            missing_items=("unverified_evidence",),
            errors=(f"{len(unverified)} plain refs rejected",),
        )

    if not verified:
        return AgentReply(
            success=False,
            content="",
            missing_items=("verified_evidence",),
        )

    # 构造 answer 文本(简化版本,真实场景会调 LLM)
    parts = []
    for r in verified:
        name = r.get("source_name") or "data"
        parts.append(f"[{name}] verified")
    return AgentReply(
        success=True,
        content=f"Based on {len(verified)} verified refs: {objective}",
        evidence=tuple(verified),
        confidence=0.85,
    )


SynthesizerAgent.run_v2 = _synthesizer_run_v2  # type: ignore[attr-defined]
