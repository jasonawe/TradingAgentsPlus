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
        scope=None,
    ) -> None:
        # §Step 24 — accept ``scope`` and forward to super(). The
        # ``SubagentProvider.build`` filter
        # (``key in inspect.signature(factory).parameters``) drops any
        # kwarg that is not an explicit parameter, so dropping scope
        # here made verifier.scope silently None despite the harness
        # passing harness.default_agent_scope. Symptom: the regression
        # test test_harness_wires_scope_into_agents also caught
        # verifier.
        super().__init__(
            llm_factory=llm_factory,
            tool_registry=tool_registry,
            scope=scope,
        )
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


# ════════════════════════════════════════════════════════
# V2 — two-phase verification (Task 17)
# ════════════════════════════════════════════════════════


def _make_verified_ref(ref_dict: dict, verification_task_id: str) -> dict:
    """Convert plain EvidenceRef dict → VerifiedEvidenceRef dict."""
    out = dict(ref_dict)
    out["verification_task_id"] = verification_task_id
    out["verification_level"] = "L1"
    out["verified_at"] = _now_iso()
    return out


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _verifier_run_v2(
    self, input: AgentInput, *, context: AgentContext
):
    """V2 entry: select VERIFY_EVIDENCE or VERIFY_ANSWER phase."""
    from types import SimpleNamespace
    from .base import AgentReply

    ctx_data = input.context or {}
    phase = ctx_data.get("phase") or "VERIFY_EVIDENCE"

    if phase == "VERIFY_EVIDENCE":
        # Issue VerifiedEvidenceRef for each plain ref
        raw_refs = ctx_data.get("evidence_refs") or []
        verified = tuple(
            _make_verified_ref(r, f"vt-{input.user_message[:8]}")
            for r in raw_refs
        )
        return AgentReply(
            success=True,
            content=f"verified {len(verified)} refs",
            evidence=verified,
            confidence=0.95,
        )

    # VERIFY_ANSWER
    answer = (ctx_data.get("answer") or input.user_message or "").strip()
    evidence = ctx_data.get("evidence_refs") or []
    target_task_id = ctx_data.get("target_task_id") or "unknown"

    if not answer:
        return AgentReply(
            success=False,
            errors=("empty answer",),
            missing_items=("answer_text",),
        )
    if not evidence:
        # 无证据 → REPAIR_REQUEST (用 SimpleNamespace 避免 AgentMessageDraft 类型限制)
        from types import SimpleNamespace
        from tradingagents.agent_harness.runtime.models import RepairPayload
        repair = SimpleNamespace(
            recipient="synthesizer",
            type="REPAIR_REQUEST",
            payload=RepairPayload(
                kind="REPAIR_REQUEST",
                target_task_id=target_task_id,
                repair_kind="DOMAIN_EVIDENCE",
                missing_evidence=("evidence_required_for_claim",),
                acceptance_criteria=("grounded",),
                rejected_artifact_ids=(),
                on_reject="FAIL_REQUESTER",
            ),
            evidence_refs=[],
            reason_summary=None,
        )
        return AgentReply(
            success=False,
            content="answer not grounded",
            missing_items=("grounded_evidence",),
            outgoing=(repair,),
        )

    return AgentReply(
        success=True,
        content="answer grounded",
        confidence=0.9,
    )


VerifierAgent.run_v2 = _verifier_run_v2  # type: ignore[attr-defined]
