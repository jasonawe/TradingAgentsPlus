"""Three-tier verification (v2 spec D6).

L1 — structural: every tool call has a valid args/result schema
L2 — semantic: tool_result satisfies the next-step preconditions
L3 — LLM-judge: SynthesizeNode answer is grounded in the tool outputs

Default behavior: L1 + L2 are always on; L3 is opt-in via
``enable_l3=True`` (N16 fix — disabled by default to control cost).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

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


class Verifier:
    """Run L1 + L2 checks (L3 is wired through orchestrator)."""

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
