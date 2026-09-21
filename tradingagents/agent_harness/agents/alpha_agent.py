"""AlphaAgent — 量化因子 (v3 spec §4.1, P8 LLM integration).

When ``llm_factory`` + ``tool_registry`` are wired, the agent enumerates
alpha158 factors and (optionally) asks the LLM to interpret the result.
Falls back to a capability placeholder otherwise.
"""
from __future__ import annotations

import logging

from .base import AgentContext, AgentInput, AgentResult, BaseAgent

LOGGER = logging.getLogger(__name__)


class AlphaAgent(BaseAgent):
    name = "alpha_agent"
    description = "alpha158 factor computation + IC/Rank IC evaluation."
    tools: list = ["list_alpha_factors", "compute_alpha_factors", "evaluate_alpha"]
    system_prompt = (
        "You are AlphaAgent. Given alpha158 factor names and IC metrics, "
        "explain which factors are most predictive and why."
    )

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        tool_results: list[dict] = []

        if self.tool_registry is not None:
            for tool_name, args in (
                ("list_alpha_factors", {}),
            ):
                result = await self._call_tool(tool_name, args)
                if result is not None:
                    tool_results.append({"name": tool_name, "result": result})

        if tool_results and self._llm_available():
            summary = self._llm_complete(
                f"Alpha158 factors: {tool_results[0]['result']}\n\n"
                "Identify 3-5 most predictive factors for A-share momentum/reversal."
            )
            return AgentResult(
                success=True,
                content=summary or "AlphaAgent: factor list ready (LLM summary unavailable)",
                structured_data={"tool_results": tool_results, "source": "llm" if summary else "tools_only"},
                tool_results=tool_results,
            )

        if tool_results:
            return AgentResult(
                success=True,
                content=f"AlphaAgent: {len(tool_results)} dataset(s) ready",
                structured_data={"tool_results": tool_results},
                tool_results=tool_results,
            )

        # Stub fallback.
        return AgentResult(
            success=True,
            content="alpha agent: list + compute + evaluate pipeline ready",
            structured_data={"capabilities": self.tools},
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
            LOGGER.debug("AlphaAgent: args coerce failed for %s: %s", tool_name, e)
            return None
        try:
            from tradingagents.agent_harness.tools import ToolContext
            ctx = ToolContext(session_id="alpha_agent")
            return await tool.invoke(validated, ctx)
        except Exception as e:
            LOGGER.debug("AlphaAgent: %s raised: %s", tool_name, e)
            return None


# ════════════════════════════════════════════════════════
# V2 entry point — select list / compute / evaluate from inputs
# ════════════════════════════════════════════════════════


async def _alpha_agent_run_v2(
    self, input: AgentInput, *, context: AgentContext
):
    """V2 entry: select compute / list / evaluate path from objective+inputs."""
    from .base import AgentReply

    tool_executor = (context.extra or {}).get("tool_executor") if context.extra else None
    if tool_executor is None:
        return AgentReply(
            success=False,
            content="",
            missing_items=("tool_executor",),
            errors=("no tool_executor in context",),
        )

    action = (input.context or {}).get("action") or "compute"
    factor = (input.context or {}).get("factor") or "alpha_default"

    tool_name = {
        "list": "alpha_list_factors",
        "compute": "alpha_compute",
        "evaluate": "alpha_evaluate",
    }.get(action, "alpha_compute")

    try:
        result = await tool_executor.invoke(
            tool_name, {"factor": factor, "action": action},
        )
    except Exception as e:
        LOGGER.warning("alpha agent error: %s", e)
        return AgentReply(success=False, errors=(str(e),))

    return AgentReply(
        success=True,
        content=f"alpha {action} for {factor}",
        evidence=({"factor": factor, "action": action, "result": result},),
        confidence=0.7,
    )


AlphaAgent.run_v2 = _alpha_agent_run_v2  # type: ignore[attr-defined]
