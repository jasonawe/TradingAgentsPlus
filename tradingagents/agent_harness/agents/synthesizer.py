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
