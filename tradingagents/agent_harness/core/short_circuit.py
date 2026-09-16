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
from .template import TemplateEngine, should_use_template  # noqa: F401

LOGGER = logging.getLogger(__name__)


class ShortCircuit:
    """Server-side executor that bypasses the StateGraph for Tier 1 queries.

    v2 spec §D1 N101 fix — Tier 1b: when ``template_engine`` is wired and
    the query matches one of ``TEMPLATE_TRIGGER_KEYWORDS`` (e.g. "说明",
    "解释"), the tool result is rendered through a Jinja2 template and
    emitted as a text string instead of raw structured data.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        template_engine: "TemplateEngine | None" = None,
    ) -> None:
        self.registry = registry
        self.template_engine = template_engine

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
            result_payload = self._safe_dump(result)
            yield ("tool_result", {"name": tool_name, "result": result_payload})
            # v2 spec §D1 N101 fix: Tier 1b — render template when query
            # matches ``TEMPLATE_TRIGGER_KEYWORDS``.  Falls back to raw
            # emit when no engine is wired or template name does not
            # match the intent.
            if self.template_engine is not None and should_use_template(message):
                tmpl_name = f"{route.intent.value}_simple"
                if self.template_engine.has(tmpl_name):
                    try:
                        text = self.template_engine.render(
                            tmpl_name, **self._ctx_for_template(result_payload),
                        )
                        yield ("agent_final", {
                            "tier": int(Tier.DIRECT),
                            "result": text,
                            "rendered": True,
                            "template": tmpl_name,
                        })
                        return
                    except Exception:
                        # Template render failed → fall through to raw emit
                        LOGGER.debug(
                            "template render failed, falling back to raw emit",
                            exc_info=True,
                        )
            yield ("agent_final", {"tier": int(Tier.DIRECT), "result": result_payload})
        except Exception as e:
            LOGGER.warning("Tier 1 short-circuit failed: %s", e)
            yield ("error", {"tier": int(Tier.DIRECT), "error": str(e)})

    @staticmethod
    def _safe_dump(obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        return obj

    @staticmethod
    def _ctx_for_template(result: Any) -> dict:
        """Flatten a tool result (dict / dict-like / Pydantic) into a
        template context dict.  Templates only see primitive values.
        """
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        if isinstance(result, dict):
            return {k: v for k, v in result.items()
                    if isinstance(v, (int, float, str, list, bool, type(None)))}
        # Fallback: wrap as a single ``value`` field
        return {"value": str(result)}

    @staticmethod
    def _tool_for_intent(intent: Intent) -> str:
        # §7.3 #12 — every read-capable intent gets a default Tier 1
        # read tool so we never emit "no Tier 1 tool for intent=NOTE"
        # warnings. Write intents still go through Tier 2 (CRUD
        # dispatch → HITL gate) because they need approval.
        return {
            Intent.QUOTE: "get_quote",
            Intent.HISTORY: "get_history",
            Intent.FUNDAMENTALS: "get_fundamentals",
            Intent.NEWS: "get_news",
            Intent.ALPHA: "list_alpha_factors",
            Intent.WATCHLIST: "list_watchlist",
            Intent.NOTE: "list_notes",
            Intent.ALERT: "list_alerts",
            Intent.SCHEDULED: "list_scheduled_tasks",
            Intent.RUN: "list_runs",
            Intent.REPORT: "list_reports",
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
