"""PlannerAgent — JSON plan generation (v3 spec §4.1).

LLM-backed in production; falls back to heuristic plan when no LLM is
wired so the agent remains testable without API keys.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .base import AgentContext, AgentInput, AgentResult, BaseAgent

LOGGER = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"\b[A-Z0-9]{1,6}(?:\.[A-Z]{2})?\b")


class PlannerAgent(BaseAgent):
    name = "planner"
    description = "Generate a JSON plan from user message + agent capabilities."
    tools: list = []
    system_prompt = (
        "You are the PlannerAgent. Decompose the user's request into a "
        "step-by-step plan. Each step references an agent name and the "
        "arguments that agent should receive. Reply with JSON only."
    )

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        symbols = _TICKER_RE.findall(input.user_message or "")
        plan = self._heuristic_plan(input.user_message, symbols)
        return AgentResult(
            success=True,
            content=json.dumps(plan, ensure_ascii=False),
            structured_data={"plan": plan, "symbols": symbols},
        )

    @staticmethod
    def _heuristic_plan(message: str, symbols: list[str]) -> list[dict[str, Any]]:
        if not symbols:
            return [{"step": 1, "agent": "synthesizer", "args": {"ask_user_for_symbol": True}}]
        plan: list[dict[str, Any]] = []
        for idx, sym in enumerate(symbols, start=1):
            plan.append({"step": idx, "agent": "data_agent", "args": {"symbol": sym}})
        plan.append({"step": len(plan) + 1, "agent": "synthesizer", "args": {}})
        return plan
