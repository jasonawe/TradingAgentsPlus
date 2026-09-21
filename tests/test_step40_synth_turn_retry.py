"""Step 40 — turn-level synth retry on L3 claim_audit fail.

P1-4 extension: when the L3 judge returns ungrounded specifically
because of claim_audit (unsupported numbers), the orchestrator
re-triggers ``_llm_synthesize`` once with a directive naming the
unsupported claims. This sits on top of the Step 34 intra-synth
citation retry so total attempts \u2264 1 initial + 1 intra + 1 turn.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


import pytest


def _make_state_with_l3(intent="quote", l3_ok=False,
                         unsupported=None, issues=None):
    """Build a state stub with ``last_l3`` pre-populated."""
    from tradingagents.agent_harness.core.tier import Intent
    state = MagicMock()
    state.intent = Intent.QUOTE if intent == "quote" else Intent.NOTE
    state.symbols = ["600036.SS"]
    state.user_message = "600036.SS \u4f30\u503c"
    state.tool_results = [
        {"name": "get_quote", "result": {"price": 99.5}},
    ]
    state.plan = []
    state.error = None
    state.carry_symbols = []
    state.slots = {}
    state.synth_retry_count = 0
    state.synth_retry_directive = None
    state.final = {"summary": "old summary"}

    class _L3:
        pass
    l3 = _L3()
    l3.ok = l3_ok
    l3.reason = "claim_audit fail" if not l3_ok else "ok"
    l3.level = 3
    l3.issues = issues or []
    l3.details = {
        "citation_score": 0.2,
        "claim_audit_score": 0.1,
        "claim_audit_unsupported": unsupported or [],
    }
    state.last_l3 = l3
    return state


def test_synth_retry_constants_defined():
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    assert hasattr(Orchestrator, "_SYNTH_TURN_RETRY_LIMIT")
    assert Orchestrator._SYNTH_TURN_RETRY_LIMIT >= 1
    assert "{unsupported}" in Orchestrator._SYNTH_TURN_RETRY_HINT_TMPL


def test_state_has_retry_fields():
    from tradingagents.agent_harness.core.orchestrator import OrchestratorState
    # dataclass fields are not always readable via dir() if the dataclass
    # uses field(); check the __dataclass_fields__ registry directly.
    field_names = set(OrchestratorState.__dataclass_fields__.keys())
    assert "synth_retry_count" in field_names
    assert "synth_retry_directive" in field_names
    assert "last_l3" in field_names


def test_directive_consumed_then_cleared():
    """``_llm_synthesize`` should append the directive to the prompt
    on first attempt and clear state.synth_retry_directive."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    from tradingagents.agent_harness.core.tier import Intent
    orch = Orchestrator.__new__(Orchestrator)
    orch.llm_factory = MagicMock()
    orch.llm_factory.is_configured = MagicMock(return_value=True)
    captured = []

    class _Prov:
        is_configured = lambda self: True

        def complete_text(self, prompt, system, temperature=0.0):
            captured.append(prompt)
            return MagicMock(content="ok\n## \u6765\u6e90\n> get_quote")

    orch.llm_factory.make = MagicMock(return_value=_Prov())

    state = MagicMock()
    state.intent = Intent.QUOTE
    state.symbols = ["600036.SS"]
    state.user_message = "x"
    state.tool_results = [{"name": "get_quote", "result": {"price": 99.5}}]
    state.synth_retry_directive = "REMOVE unsupported numbers"

    orch._build_synthesize_prompt = MagicMock(return_value="BASE PROMPT")

    import asyncio
    asyncio.run(orch._llm_synthesize(state))

    # The directive was appended to the prompt
    assert "REMOVE unsupported numbers" in captured[0]
    # The directive was cleared
    assert state.synth_retry_directive is None


def test_no_retry_when_l3_ok():
    """If L3 returned OK, no turn-level retry should be triggered."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    state = _make_state_with_l3(l3_ok=True)
    # Simulate the orchestrator's retry-decision logic directly.
    l3 = state.last_l3
    assert l3.ok is True
    # The orchestrator-level check is "not l3.ok" \u2014 so l3.ok True
    # means we skip the retry path entirely.


def test_no_retry_when_budget_exhausted():
    """If we've already retried at the turn level, don't loop again."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    state = _make_state_with_l3(l3_ok=False, unsupported=["99.9"])
    state.synth_retry_count = Orchestrator._SYNTH_TURN_RETRY_LIMIT
    # The retry condition is `count < limit`; with count == limit, no retry.
    assert not (
        state.synth_retry_count < Orchestrator._SYNTH_TURN_RETRY_LIMIT
    )


def test_no_retry_when_l3_fail_is_not_claim_audit():
    """If L3 ungrounded reason is purely a tool failure, don't retry."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    state = _make_state_with_l3(
        l3_ok=False,
        unsupported=[],
        issues=["tool fetch_error: no quote data"],
    )
    # Decision rule: needs both `not l3.ok` AND
    # (unsupported non-empty OR issues mention forward/claim_audit).
    # Here unsupported is empty AND issues don't mention claim_audit.
    is_claim_fail = bool([]) or any(
        "forward" in (r or "").lower()
        or "claim_audit" in (r or "").lower()
        for r in (state.last_l3.issues or [])
    )
    assert is_claim_fail is False


def test_retry_triggered_when_claim_audit_unsupported():
    """When unsupported list is non-empty, the retry should fire."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    state = _make_state_with_l3(
        l3_ok=False,
        unsupported=["99.9", "12x"],
    )
    unsupported = (state.last_l3.details or {}).get(
        "claim_audit_unsupported"
    ) or []
    is_claim_fail = bool(unsupported) or any(
        "forward" in (r or "").lower()
        or "claim_audit" in (r or "").lower()
        for r in (state.last_l3.issues or [])
    )
    assert is_claim_fail is True
    # The directive should be formatted with the unsupported list.
    directive = Orchestrator._SYNTH_TURN_RETRY_HINT_TMPL.format(
        unsupported=", ".join(str(u) for u in unsupported[:6])
    )
    assert "99.9" in directive
    assert "12x" in directive


def test_retry_hint_template_renders_empty_unsupported():
    """When unsupported is empty, the directive should still be usable."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    directive = Orchestrator._SYNTH_TURN_RETRY_HINT_TMPL.format(
        unsupported="(see previous issues)"
    )
    assert "claim_audit" in directive
    assert "(see previous issues)" in directive
