"""DataAgent — 行情 + 基本面 (v3 spec §4.1, N2/C1 fix).

合并 QuoteAgent + FundamentalsAgent; 不含 get_history (属于 AlphaAgent 前置)。

Tier 3 DAG 的并行节点之一;Tier 1 短路径**不**经过 DataAgent
(直接 PROVIDERS.get_quote)。

P8 LLM integration: when ``llm_factory`` + ``tool_registry`` are wired,
the agent pulls real quotes/fundamentals via the registered tools and
optionally asks the LLM to summarize them. Falls back to a structured
placeholder otherwise.
"""
from __future__ import annotations

import asyncio
import logging

from .base import AgentContext, AgentInput, AgentResult, BaseAgent

LOGGER = logging.getLogger(__name__)


class DataAgent(BaseAgent):
    name = "data_agent"
    description = "Fetch quote + fundamentals for one symbol (no history)."
    tools: list = ["get_quote", "get_quotes_batch", "get_fundamentals"]
    system_prompt = (
        "You are DataAgent. Given a symbol's quote and fundamentals, "
        "summarize key numbers (price, change %, P/E, market cap) in 1-2 sentences."
    )

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        symbol = (
            (input.context or {}).get("symbol")
            or (input.plan_step or {}).get("args", {}).get("symbol", "")
        )
        if not symbol:
            return AgentResult(
                success=False,
                content="DataAgent: no symbol provided",
                errors=["symbol_missing"],
            )

        tool_results: list[dict] = []

        # Real path: invoke tool_registry tools with proper schema validation.
        if self.tool_registry is not None:
            specs = (
                ("get_quote", {"symbol": symbol}),
                ("get_fundamentals", {"symbol": symbol}),
            )
            # Run independent read tools concurrently — quote + fundamentals
            # have no data dependency on each other, so serial awaits would
            # just stack latency. ``asyncio.gather`` preserves the order of
            # ``specs`` even when one tool finishes first; per-tool failures
            # are swallowed inside ``_call_tool`` (returns ``None``).
            results = await asyncio.gather(
                *(self._call_tool(name, args) for name, args in specs)
            )
            for (tool_name, _), result in zip(specs, results):
                if result is not None:
                    tool_results.append({"name": tool_name, "result": result})

        # LLM summarize path (only if we have data + LLM).
        if tool_results and self._llm_available():
            summary = self._llm_summarize(symbol, tool_results)
            return AgentResult(
                success=True,
                content=summary or "DataAgent: data fetched (LLM summary unavailable)",
                structured_data={"symbol": symbol, "tool_results": tool_results},
                tool_results=tool_results,
                source="llm" if summary else "tools_only",
            )

        if tool_results:
            return AgentResult(
                success=True,
                content=f"DataAgent: fetched {len(tool_results)} datasets for {symbol}",
                structured_data={"symbol": symbol, "tool_results": tool_results},
                tool_results=tool_results,
            )

        # Stub fallback.
        return AgentResult(
            success=bool(symbol),
            content=f"data agent ready for {symbol or 'unknown symbol'}",
            structured_data={"symbol": symbol},
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _call_tool(self, tool_name: str, args_dict: dict):
        """Invoke a registered tool with schema validation; return None on failure."""
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
            LOGGER.debug("DataAgent: args coerce failed for %s: %s", tool_name, e)
            return None
        try:
            # Minimal ToolContext — most read tools don't need it.
            from tradingagents.agent_harness.tools import ToolContext
            ctx = ToolContext(session_id="data_agent")
            return await tool.invoke(validated, ctx)
        except Exception as e:
            LOGGER.debug("DataAgent: %s raised: %s", tool_name, e)
            return None

    def _llm_summarize(self, symbol: str, tool_results: list[dict]) -> str | None:
        import json as _json
        prompt = (
            f"Symbol: {symbol}\n\n"
            f"Tool results: {_json.dumps(tool_results, ensure_ascii=False, default=str)[:4000]}\n\n"
            "Write a 1-2 sentence summary in the same language as the symbol description "
            "(Chinese for A-share codes like 600xxx.SH/600xxx.SS, English otherwise)."
        )
        return self._llm_complete(prompt)
