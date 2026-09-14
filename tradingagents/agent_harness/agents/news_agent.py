"""NewsAgent — 新闻舆情 (v3 spec §4.1, P8 LLM integration).

When ``llm_factory`` + ``tool_registry`` are wired, the agent fetches
news headlines via ``get_news`` and (optionally) asks the LLM to produce
a sentiment summary. Falls back to a stub otherwise.
"""
from __future__ import annotations

import logging

from .base import AgentContext, AgentInput, AgentResult, BaseAgent

LOGGER = logging.getLogger(__name__)


class NewsAgent(BaseAgent):
    name = "news_agent"
    description = "Recent news headlines + sentiment summary."
    tools: list = ["get_news"]
    system_prompt = (
        "You are NewsAgent. Given recent news headlines for a symbol, "
        "summarize sentiment (positive / neutral / negative) and the "
        "dominant themes in 1-2 sentences."
    )

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        symbol = (input.context or {}).get("symbol", "")
        days = (input.context or {}).get("days", 7)
        tool_results: list[dict] = []

        if self.tool_registry is not None and symbol:
            result = await self._call_tool("get_news", {"symbol": symbol, "days": days})
            if result is not None:
                tool_results.append({"name": "get_news", "result": result})

        if tool_results and self._llm_available():
            summary = self._llm_complete(
                f"Symbol: {symbol}\nNews: {tool_results[0]['result']}\n\n"
                "Summarize sentiment + dominant themes in 1-2 sentences."
            )
            return AgentResult(
                success=True,
                content=summary or "NewsAgent: news fetched (LLM summary unavailable)",
                structured_data={"symbol": symbol, "tool_results": tool_results},
                tool_results=tool_results,
                source="llm" if summary else "tools_only",
            )

        if tool_results:
            return AgentResult(
                success=True,
                content=f"NewsAgent: fetched news for {symbol or 'unknown'}",
                structured_data={"symbol": symbol, "tool_results": tool_results},
                tool_results=tool_results,
            )

        # Stub fallback.
        return AgentResult(
            success=True,
            content=f"news agent ready for {symbol or 'unknown'}",
            structured_data={"symbol": symbol},
        )

    async def _call_tool(self, tool_name: str, args_dict: dict):
        try:
            tool = self.tool_registry.get(tool_name)
        except KeyError:
            return None
        try:
            schema_cls = getattr(tool.schema, "args_schema", None)
            if schema_cls is not None and isinstance(args_dict, dict) and hasattr(schema_cls, "model_validate"):
                validated = schema_cls.model_validate(args_dict)
            else:
                validated = args_dict
        except Exception as e:
            LOGGER.debug("NewsAgent: args coerce failed for %s: %s", tool_name, e)
            return None
        try:
            from tradingagents.agent_harness.tools import ToolContext
            ctx = ToolContext(session_id="news_agent")
            return await tool.invoke(validated, ctx)
        except Exception as e:
            LOGGER.debug("NewsAgent: %s raised: %s", tool_name, e)
            return None
