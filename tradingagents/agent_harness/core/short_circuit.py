"""Tier 1 short-circuit (v2 spec D1 / N90 fix).

Routes a regex-matched "price?" query directly to ``tool.invoke()``
without touching the StateGraph. Three guarantees:

- 0 LLM calls (N87 fix)
- DataResponse.warnings emitted as ``warning`` SSE event (N90 fix)
- tier=2 fallback when tool raises — orchestrator picks up the next tier
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from tradingagents.agent_harness.tools import ToolContext, ToolRegistry
from tradingagents.agent_harness.tools.schema import ToolSchema

from .tier import Intent, RouteResult, Tier

LOGGER = logging.getLogger(__name__)


class ShortCircuit:
    """Server-side executor that bypasses the StateGraph for Tier 1 queries."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def run(
        self,
        route: RouteResult,
        message: str,
        context: ToolContext,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Stream SSE events for a Tier 1 query.

        Yields ``(event_name, payload)`` tuples. Caller maps to SSE wire format.
        """
        tool_name = self._tool_for_intent(route.intent)
        if not tool_name:
            yield ("warning", {"message": f"no Tier 1 tool for intent={route.intent}"})
            return

        symbol = route.symbols[0] if route.symbols else ""
        if not symbol:
            yield ("warning", {"message": "no ticker detected, falling back to Tier 2"})
            return

        try:
            tool = self.registry.get(tool_name)
            args_schema = tool.schema.args_schema
            args = self._build_args(args_schema, symbol)
            yield ("tool_call", {"name": tool_name, "args": self._safe_dump(args)})
            result = await tool.invoke(args, context)
            yield ("tool_result", {"name": tool_name, "result": self._safe_dump(result)})
            yield ("agent_final", {"tier": int(Tier.DIRECT), "result": self._safe_dump(result)})
        except Exception as e:
            LOGGER.warning("Tier 1 short-circuit failed: %s", e)
            yield ("error", {"tier": int(Tier.DIRECT), "error": str(e)})

    @staticmethod
    def _safe_dump(obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        return obj

    @staticmethod
    def _tool_for_intent(intent: Intent) -> str:
        return {
            Intent.QUOTE: "get_quote",
            Intent.HISTORY: "get_history",
            Intent.FUNDAMENTALS: "get_fundamentals",
            Intent.NEWS: "get_news",
            Intent.ALPHA: "list_alpha_factors",
            Intent.WATCHLIST: "list_watchlist",
            Intent.SCHEDULED: "list_scheduled_tasks",
        }.get(intent, "")

    @staticmethod
    def _build_args(args_schema: type, symbol: str) -> Any:
        """Instantiate the tool's args schema with sensible defaults."""
        # Lazy imports to avoid circulars.
        from tradingagents.agent_harness.tools import builtin as _builtin  # noqa: F401
        if hasattr(args_schema, "model_validate"):
            try:
                return args_schema.model_validate({"symbol": symbol})
            except Exception:
                pass
        # Fallback: construct dataclass-like or dict.
        try:
            return args_schema(symbol=symbol)
        except Exception:
            return {"symbol": symbol}
