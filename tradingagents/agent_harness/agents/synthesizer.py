"""SynthesizerAgent — 回答合成 (v3 spec §4.1)."""
from __future__ import annotations

from .base import AgentContext, AgentInput, AgentResult, BaseAgent


class SynthesizerAgent(BaseAgent):
    name = "synthesizer"
    description = "Synthesize a natural-language answer from verified tool results."
    tools: list = []
    system_prompt = "你是 SynthesizerAgent,基于已验证数据合成回答。"

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        tool_results = (input.context or {}).get("tool_results", [])
        symbols = (input.context or {}).get("symbols", [])
        content_parts = [f"processed {len(tool_results)} tool_results"]
        if symbols:
            content_parts.append(f"for {', '.join(symbols)}")
        return AgentResult(
            success=True,
            content=" — ".join(content_parts),
            structured_data={"tool_count": len(tool_results), "symbols": symbols},
        )
