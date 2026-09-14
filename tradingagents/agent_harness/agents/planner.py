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

_PLAN_SYSTEM = (
    "You are a finance research planner. Reply ONLY with valid JSON. "
    "No commentary, no markdown fences. Output schema: "
    "[{\"step\": <int>, \"agent\": <agent_name>, \"args\": {<dict>}}]"
)


class PlannerAgent(BaseAgent):
    name = "planner"
    description = "Generate a JSON plan from user message + agent capabilities."
    tools: list = []
    system_prompt = _PLAN_SYSTEM

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        symbols = _TICKER_RE.findall(input.user_message or "")

        # LLM path: ask the model for a structured JSON plan.
        if self._llm_available():
            caps_lines = []
            for name in self._agent_caps():
                caps_lines.append(f"- {name['agent']}: {name['capability']}")
            prompt = (
                f"User message: {input.user_message}\n\n"
                f"Detected symbols: {symbols}\n\n"
                "Available agents:\n" + "\n".join(caps_lines) +
                "\n\nGenerate a JSON plan as a list of {step, agent, args} objects."
            )
            content = self._llm_complete(prompt, temperature=0.0)
            plan = self._parse_plan(content) if content else []
            if plan:
                return AgentResult(
                    success=True,
                    content=json.dumps(plan, ensure_ascii=False),
                    structured_data={"plan": plan, "symbols": symbols, "source": "llm"},
                )
            LOGGER.debug("PlannerAgent: LLM plan unusable, falling back to heuristic")

        # Heuristic fallback.
        plan = self._heuristic_plan(input.user_message, symbols)
        return AgentResult(
            success=True,
            content=json.dumps(plan, ensure_ascii=False),
            structured_data={"plan": plan, "symbols": symbols, "source": "heuristic"},
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

    @staticmethod
    def _parse_plan(content: str | None) -> list[dict[str, Any]]:
        if not content:
            return []
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except Exception:
            LOGGER.warning("PlannerAgent: LLM plan returned non-JSON: %s", content[:120])
        return []

    def _agent_caps(self) -> list[dict[str, Any]]:
        """Read capabilities from the agent_registry if we can find one.

        The registry isn't injected by default; subclasses (or wiring code)
        can stash a reference via ``self.tool_registry._agent_registry`` if
        they want richer prompts. We keep the fallback short.
        """
        reg = getattr(self, "_agent_registry", None)
        if reg is None:
            # Best-effort: derive from own knowledge of names. Keep minimal.
            return [
                {"agent": "data_agent", "capability": "quote + fundamentals"},
                {"agent": "alpha_agent", "capability": "alpha158 factor compute + IC"},
                {"agent": "news_agent", "capability": "news + sentiment"},
                {"agent": "synthesizer", "capability": "synthesize final answer"},
            ]
        return reg.plan_capabilities()
