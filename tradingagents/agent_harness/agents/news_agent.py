"""NewsAgent — 新闻舆情 (v3 spec §4.1)."""
from __future__ import annotations

from .base import AgentContext, AgentInput, AgentResult, BaseAgent


class NewsAgent(BaseAgent):
    name = "news_agent"
    description = "Recent news headlines + sentiment summary."
    tools: list = ["get_news"]
    system_prompt = "你是 NewsAgent,擅长新闻舆情分析。"

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        symbol = (input.context or {}).get("symbol", "")
        return AgentResult(
            success=True,
            content=f"news agent ready for {symbol or 'unknown'}",
            structured_data={"symbol": symbol},
        )
