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
                "Summarize sentiment + dominant themes in 1-2 sentences.",
                mode="quick",
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


# ════════════════════════════════════════════════════════
# V2 entry point — lookback freshness validation
# ════════════════════════════════════════════════════════


async def _news_agent_run_v2(
    self, input: AgentInput, *, context: AgentContext
):
    """V2 entry: validate as_of / lookback, fetch via ToolExecutor."""
    from datetime import datetime, timezone
    from .base import AgentReply

    tool_executor = (context.extra or {}).get("tool_executor") if context.extra else None
    if tool_executor is None:
        return AgentReply(
            success=False,
            content="",
            missing_items=("tool_executor",),
            errors=("no tool_executor in context",),
        )

    symbols = (input.context or {}).get("symbols") or []
    lookback_days = int((input.context or {}).get("lookback_days") or 7)
    as_of_str = (input.context or {}).get("as_of")

    try:
        as_of = datetime.fromisoformat(as_of_str) if as_of_str else datetime.now(timezone.utc)
    except Exception:
        as_of = datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    age_days = (now - as_of).days
    missing: list[str] = []
    if age_days > lookback_days:
        missing.append(f"as_of older than lookback ({age_days}d > {lookback_days}d)")

    evidence = []
    for sym in symbols:
        try:
            res = await tool_executor.invoke(
                "get_news", {"symbol": sym, "lookback_days": lookback_days},
            )
        except Exception as e:
            LOGGER.warning("news agent error for %s: %s", sym, e)
            continue
        evidence.append({"symbol": sym, "items": res.get("items", []) if isinstance(res, dict) else []})

    return AgentReply(
        success=True,
        content=f"news for {len(evidence)} symbol(s)",
        evidence=tuple(evidence),
        confidence=0.8 if evidence else 0.0,
        missing_items=tuple(missing),
    )


NewsAgent.run_v2 = _news_agent_run_v2  # type: ignore[attr-defined]
