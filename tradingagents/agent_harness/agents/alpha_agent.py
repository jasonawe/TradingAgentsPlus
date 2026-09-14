"""AlphaAgent — 量化因子 (v3 spec §4.1)."""
from __future__ import annotations

from .base import AgentContext, AgentInput, AgentResult, BaseAgent


class AlphaAgent(BaseAgent):
    name = "alpha_agent"
    description = "alpha158 factor computation + IC/Rank IC evaluation."
    tools: list = ["list_alpha_factors", "compute_alpha_factors", "evaluate_alpha"]
    system_prompt = "你是 AlphaAgent,擅长 alpha158 量化因子。"

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        return AgentResult(
            success=True,
            content="alpha agent: list + compute + evaluate pipeline ready",
            structured_data={"capabilities": self.tools},
        )
