"""Three-tier verification (v2 spec D6).

L1 — structural: every tool call has a valid args/result schema
L2 — semantic: tool_result satisfies the next-step preconditions
L3 — LLM-judge: SynthesizeNode answer is grounded in the tool outputs

Default behavior: L1 + L2 are always on; L3 is opt-in via
``enable_l3=True`` (N16 fix — disabled by default to control cost).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field

LOGGER = logging.getLogger(__name__)


class VerificationLevel(IntEnum):
    L1_STRUCTURAL = 1
    L2_SEMANTIC = 2
    L3_LLM_JUDGE = 3


@dataclass
class VerificationResult:
    ok: bool
    level: VerificationLevel
    reason: str = ""
    details: dict[str, Any] | None = None


class LLMJudgeVerdict(BaseModel):
    """L3 LLM-judge verdict (spec §D6, C5 fix).

    Score 0.0-1.0; >= 0.7 is treated as grounded. Issues describe concrete
    problems (made-up numbers, off-topic, etc.); suggestion is the
    recommended remediation (replan, fetch more tools, etc.).
    """

    score: float = Field(..., ge=0.0, le=1.0, description="groundedness 评分,>= 0.7 视为 grounded")
    issues: list[str] = Field(default_factory=list, description="具体问题")
    suggestion: str = Field("", description="改进建议")
    reasoning: str = Field("", description="judge 的推理过程")

    @property
    def grounded(self) -> bool:
        return self.score >= 0.7


_JUDGE_SYSTEM = (
    "You are an answer-groundedness judge. Evaluate whether the LLM "
    "answer is actually grounded in the provided tool results (vs. "
    "hallucinated). Reply ONLY with valid JSON matching the schema. "
    "No commentary, no markdown fences."
)

_JUDGE_THRESHOLD = 0.7


class Verifier:
    """Run L1 + L2 checks (L3 needs a judge factory)."""

    def __init__(self, *, judge_factory: Any | None = None) -> None:
        self.judge_factory = judge_factory

    def verify_l1(self, tool_call: dict[str, Any], tool_result: Any) -> VerificationResult:
        if not tool_call or "name" not in tool_call:
            return VerificationResult(False, VerificationLevel.L1_STRUCTURAL, "missing tool name")
        if tool_result is None:
            return VerificationResult(False, VerificationLevel.L1_STRUCTURAL, "null tool result")
        return VerificationResult(True, VerificationLevel.L1_STRUCTURAL, "ok")

    def verify_l2(self, intent: str, tool_results: list[dict[str, Any]]) -> VerificationResult:
        if intent == "quote" and not tool_results:
            return VerificationResult(False, VerificationLevel.L2_SEMANTIC, "quote returned no data")
        warnings = [r for r in tool_results if isinstance(r, dict) and r.get("warnings")]
        if warnings and intent not in {"quote", "history"}:
            return VerificationResult(
                False, VerificationLevel.L2_SEMANTIC,
                f"{len(warnings)} warnings present",
                details={"warnings": warnings},
            )
        return VerificationResult(True, VerificationLevel.L2_SEMANTIC, "ok")

    # ------------------------------------------------------------------
    # L3 LLM-judge (spec §D6)
    # ------------------------------------------------------------------
    def should_run_l3(self, tool_results: list[dict[str, Any]], *, enable_l3: bool) -> bool:
        """L3 enable condition (N12 fix): tool_results > 4 AND user toggled on."""
        if not enable_l3:
            return False
        if len(tool_results) <= 4:
            return False
        if self.judge_factory is None or not self.judge_factory.is_configured():
            return False
        return True

    async def verify_l3(
        self,
        *,
        user_query: str,
        tool_results: list[dict[str, Any]],
        llm_answer: str,
    ) -> VerificationResult:
        """Run the LLM-judge and return a VerificationResult.

        On any LLM failure (parse error / network / refusal), fail OPEN
        — L1+L2 are the source of truth, L3 is an extra signal only.
        """
        if self.judge_factory is None or not self.judge_factory.is_configured():
            return VerificationResult(
                True,
                VerificationLevel.L3_LLM_JUDGE,
                "judge factory not configured — skipping L3 (fail-open)",
            )
        try:
            provider = self.judge_factory.make()
            prompt = self._build_judge_prompt(user_query, tool_results, llm_answer)
            response = provider.complete_text(
                prompt=prompt, system=_JUDGE_SYSTEM, temperature=0.0
            )
            content = getattr(response, "content", response)
            verdict = self._parse_verdict(content)
            if verdict is None:
                return VerificationResult(
                    True,
                    VerificationLevel.L3_LLM_JUDGE,
                    "judge returned non-JSON — failing open",
                    details={"raw": content[:200]},
                )
            # §Step 22 P1 — citation score. We extract the set of tool
            # names that contributed data to the answer and score how
            # many of them the LLM cited. The score is informational
            # only — we never block on it because the LLM-judge is the
            # authoritative grounded/ ungrounded verdict. We surface
            # it in ``details`` so the UI can show a citation badge
            # alongside the LLM-judge score.
            try:
                from tradingagents.agent_harness.verification.citations import (
                    citation_score as _citation_score,
                )
                cited_tools = [
                    str(r.get("name") or r.get("tool") or "")
                    for r in (tool_results or [])
                    if isinstance(r, dict)
                ]
                # Drop falsy / unknown names.
                cited_tools = [t for t in cited_tools if t]
                cite = _citation_score(llm_answer or "", cited_tools)
            except Exception:
                cite = 1.0
            details = {
                "score": verdict.score,
                "issues": verdict.issues,
                "suggestion": verdict.suggestion,
                "reasoning": verdict.reasoning,
                "threshold": _JUDGE_THRESHOLD,
                "citation_score": cite,
            }
            if verdict.grounded:
                return VerificationResult(
                    True,
                    VerificationLevel.L3_LLM_JUDGE,
                    f"grounded (score={verdict.score:.2f})",
                    details=details,
                )
            return VerificationResult(
                False,
                VerificationLevel.L3_LLM_JUDGE,
                f"ungrounded (score={verdict.score:.2f}): {'; '.join(verdict.issues) or verdict.reasoning[:120]}",
                details=details,
            )
        except Exception as e:
            LOGGER.warning("L3 judge failed, failing open: %s", e)
            return VerificationResult(
                True,
                VerificationLevel.L3_LLM_JUDGE,
                f"judge exception: {e} — failing open",
            )

    @staticmethod
    def _build_judge_prompt(user_query: str, tool_results: list[dict[str, Any]], llm_answer: str) -> str:
        results_summary = json.dumps(tool_results, ensure_ascii=False, default=str)[:4000]
        return (
            f"user_query: {user_query}\n\n"
            f"tool_results: {results_summary}\n\n"
            f"llm_answer: {llm_answer[:2000]}\n\n"
            "输出 JSON: "
            '{"score": 0.0-1.0, "issues": [...], "suggestion": "...", "reasoning": "..."}'
        )

    @staticmethod
    def _parse_verdict(content: str | None) -> LLMJudgeVerdict | None:
        if not content:
            return None
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return LLMJudgeVerdict.model_validate(data)
        except Exception as e:
            LOGGER.warning("L3 judge returned non-JSON content: %s (%s)", text[:120], e)
        return None
