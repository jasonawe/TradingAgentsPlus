"""VerifierAgent — L1/L2/L3 verification (v3 spec §4.1, §D6).

Default behavior: structural + semantic checks (no LLM). When
``enable_l3=True`` AND ``tool_results`` > 4, the agent asks the
``judge_factory`` (a separate LLMFactory) to evaluate groundedness.
"""
from __future__ import annotations

import json as _json
import logging

from .base import AgentContext, AgentInput, AgentResult, BaseAgent

LOGGER = logging.getLogger(__name__)


class VerifierAgent(BaseAgent):
    name = "verifier"
    description = "L1/L2 structural + L3 LLM-judge for tool_results and final answers (D6)."
    tools: list = []

    def __init__(
        self,
        *,
        llm_factory=None,
        tool_registry=None,
        judge_factory=None,
        enable_l3: bool = False,
    ) -> None:
        super().__init__(llm_factory=llm_factory, tool_registry=tool_registry)
        self.judge_factory = judge_factory
        self.enable_l3 = enable_l3

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        tool_results = (input.context or {}).get("tool_results", [])
        llm_answer = (input.context or {}).get("llm_answer", "")

        errors = [r for r in tool_results if isinstance(r, dict) and r.get("error")]
        warnings = [r for r in tool_results if isinstance(r, dict) and r.get("warnings")]

        # L1 + L2 structural checks (always on). Keep "all checks passed"
        # text for backward-compat with existing tests; L3 verdict is
        # appended after when applicable.
        reason_parts: list[str] = []
        ok = len(errors) == 0
        if errors:
            reason_parts.append(f"{len(errors)} tool errors")
        if warnings:
            reason_parts.append(f"{len(warnings)} warnings")
        if not reason_parts:
            reason_parts.append("all checks passed")

        structured: dict = {"errors": errors, "warnings": warnings}

        # L3 LLM-judge (opt-in, spec §D6).
        if (
            self.enable_l3
            and self.judge_factory is not None
            and self.judge_factory.is_configured()
            and len(tool_results) > 4
        ):
            try:
                from tradingagents.agent_harness.core.verification import (
                    LLMJudgeVerdict,
                )
                provider = self.judge_factory.make()
                prompt = (
                    f"user_query: {input.user_message}\n"
                    f"tool_results: {_json.dumps(tool_results, ensure_ascii=False, default=str)[:4000]}\n"
                    f"llm_answer: {llm_answer[:2000]}\n\n"
                    "Output JSON: "
                    '{"score": 0.0-1.0, "issues": [...], "suggestion": "...", "reasoning": "..."}'
                )
                response = provider.complete_text(
                    prompt=prompt,
                    system=(
                        "You are an answer-groundedness judge. Evaluate whether "
                        "the LLM answer is actually grounded in the provided tool "
                        "results (vs. hallucinated). Reply ONLY with valid JSON."
                    ),
                    temperature=0.0,
                )
                content = getattr(response, "content", response)
                try:
                    data = _json.loads(_strip_fence(content or ""))
                    verdict = LLMJudgeVerdict.model_validate(data)
                    structured["l3_verdict"] = verdict.model_dump()
                    if not verdict.grounded:
                        ok = False
                        reason_parts.append(
                            f"L3 ungrounded (score={verdict.score:.2f}): {'; '.join(verdict.issues) or verdict.reasoning[:120]}"
                        )
                    else:
                        reason_parts.append(f"L3 grounded (score={verdict.score:.2f})")
                except Exception as e:
                    LOGGER.debug("VerifierAgent: L3 non-JSON response, skipping: %s", e)
                    reason_parts.append("L3 skip (non-JSON)")
            except Exception as e:
                LOGGER.debug("VerifierAgent: L3 judge failed, skipping: %s", e)
                reason_parts.append("L3 skip (judge failed)")

        return AgentResult(
            success=ok,
            content=" | ".join(reason_parts),
            structured_data=structured,
        )


def _strip_fence(text: str) -> str:
    import re
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"```\s*$", "", text)
    return text
