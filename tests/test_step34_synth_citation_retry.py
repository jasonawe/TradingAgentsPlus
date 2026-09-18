"""Step 34 — Synthesize citation retry loop.

When the first LLM synthesize pass produces an answer without a
citation block AND data tools were used, the orchestrator retries
once with a strict hint appended. The retry budget is configurable
via Orchestrator._CITATION_RETRY_LIMIT.

Trivial CRUD acks (no data tools) skip retry entirely — there's
nothing to cite.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _make_state(intent="quote", tool_results=None, user_message="600036.SS"):
    from tradingagents.agent_harness.core.tier import Intent
    state = MagicMock()
    state.intent = Intent.QUOTE if intent == "quote" else Intent.NOTE
    state.symbols = ["600036.SS"]
    state.user_message = user_message
    state.tool_results = tool_results or [
        {"name": "get_quote", "result": {"price": 99.5}},
    ]
    state.plan = []
    state.error = None
    state.carry_symbols = []
    state.slots = {}
    return state


def _make_provider(responses):
    """Mock LLM provider that returns each response in order, then repeats last.

    Exposes ``complete_call_count`` so tests can verify retry behavior.
    """
    p = MagicMock()
    p.is_configured = MagicMock(return_value=True)
    p.complete_call_count = 0
    iterator = iter(responses)

    def _complete(prompt, system, temperature=0.0):
        p.complete_call_count += 1
        try:
            return next(iterator)
        except StopIteration:
            return responses[-1]
    p.complete_text = _complete
    return p


async def _run_synth(orch, state):
    return await orch._llm_synthesize(state)


def test_retry_when_first_answer_missing_citation():
    """First answer has no 来源 block. Orchestrator retries once with hint."""
    import asyncio
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)  # skip __init__
    orch.llm_factory = MagicMock()
    orch.llm_factory.is_configured = MagicMock(return_value=True)
    bad_answer = "## 数据事实\n股价 99.5"  # NO citation block
    good_answer = "## 数据事实\n股价 99.5\n## 来源\n> get_quote"
    orch.llm_factory.make = MagicMock(return_value=_make_provider([
        MagicMock(content=bad_answer),
        MagicMock(content=good_answer),
    ]))
    state = _make_state(tool_results=[{"name": "get_quote", "result": {"price": 99.5}}])
    result = asyncio.run(_run_synth(orch, state))
    assert "来源" in result["summary"]
    # 1 original + 1 retry = 2 LLM calls
    provider = orch.llm_factory.make.return_value
    assert provider.complete_call_count == 2


def test_no_retry_when_first_answer_has_citation():
    """First answer already has 来源. No retry should happen."""
    import asyncio
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.llm_factory = MagicMock()
    orch.llm_factory.is_configured = MagicMock(return_value=True)
    good_answer = "## 数据事实\n股价 99.5\n## 来源\n> get_quote"
    provider = _make_provider([MagicMock(content=good_answer)])
    orch.llm_factory.make = MagicMock(return_value=provider)
    state = _make_state(tool_results=[{"name": "get_quote", "result": {"price": 99.5}}])
    result = asyncio.run(_run_synth(orch, state))
    assert result["summary"] == good_answer
    # Single call — no retry
    provider = orch.llm_factory.make.return_value
    assert provider.complete_call_count == 1


def test_no_retry_when_only_crud_tools_used():
    """Trivial CRUD ack — no data tools — skip retry entirely."""
    import asyncio
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.llm_factory = MagicMock()
    orch.llm_factory.is_configured = MagicMock(return_value=True)
    bad_answer = "笔记已删除"  # no citation, but no data tools either
    orch.llm_factory.make = MagicMock(return_value=_make_provider([
        MagicMock(content=bad_answer),
    ]))
    state = _make_state(intent="note", tool_results=[
        {"name": "delete_note", "result": {"status": "deleted"}},
    ])
    result = asyncio.run(_run_synth(orch, state))
    # Single call (no retry)
    provider = orch.llm_factory.make.return_value
    assert provider.complete_call_count == 1
    assert result["summary"] == bad_answer


def test_retry_respects_budget():
    """Even when citation keeps failing, we stop after _CITATION_RETRY_LIMIT."""
    import asyncio
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.llm_factory = MagicMock()
    orch.llm_factory.is_configured = MagicMock(return_value=True)
    # Both attempts missing citation
    orch.llm_factory.make = MagicMock(return_value=_make_provider([
        MagicMock(content="bad 1"),
        MagicMock(content="bad 2"),
    ]))
    state = _make_state(tool_results=[{"name": "get_quote", "result": {"price": 99.5}}])
    result = asyncio.run(_run_synth(orch, state))
    provider = orch.llm_factory.make.return_value
    # 1 original + budget retries
    assert provider.complete_call_count == 1 + Orchestrator._CITATION_RETRY_LIMIT


def test_retry_on_forward_claim_with_partial_citation():
    """When answer has a forward-looking claim ('预计'), retry even if
    citation is partially present."""
    import asyncio
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.llm_factory = MagicMock()
    orch.llm_factory.is_configured = MagicMock(return_value=True)
    # First answer: has citation but also forward claim
    first_answer = (
        "## 数据事实\n股价 99.5\n## 来源\n> get_quote\n"
        "## 方向性建议\n预计 PE 将达 12x"  # forward claim
    )
    orch.llm_factory.make = MagicMock(return_value=_make_provider([
        MagicMock(content=first_answer),
        MagicMock(content="## 数据事实\n股价 99.5\n## 来源\n> get_quote"),
    ]))
    state = _make_state(tool_results=[{"name": "get_quote", "result": {"price": 99.5}}])
    result = asyncio.run(_run_synth(orch, state))
    provider = orch.llm_factory.make.return_value
    # 1 original + 1 retry (because forward claim with citation < 0.8)
    assert provider.complete_call_count == 2


def test_provider_failure_with_no_prior_attempt_returns_placeholder():
    """If the very first LLM call fails, fall back to the standard placeholder."""
    import asyncio
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.llm_factory = MagicMock()
    orch.llm_factory.is_configured = MagicMock(return_value=True)

    class _BrokenProvider:
        is_configured = lambda self: True
        def complete_text(self, prompt, system, temperature=0.0):
            raise ConnectionError("network down")

    orch.llm_factory.make = MagicMock(return_value=_BrokenProvider())
    state = _make_state()
    result = asyncio.run(_run_synth(orch, state))
    assert "LLM synthesize failed" in result["summary"]


def test_provider_failure_after_good_attempt_keeps_good_answer():
    """If the retry fails, the orchestrator should keep the first answer."""
    import asyncio
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.llm_factory = MagicMock()
    orch.llm_factory.is_configured = MagicMock(return_value=True)

    class _SometimesBroken:
        is_configured = lambda self: True
        call_count = 0

        def complete_text(self, prompt, system, temperature=0.0):
            self.call_count += 1
            if self.call_count == 1:
                return MagicMock(content="bad first answer")
            raise ConnectionError("retry failed")

    provider = _SometimesBroken()
    orch.llm_factory.make = MagicMock(return_value=provider)
    state = _make_state(tool_results=[{"name": "get_quote", "result": {"price": 99.5}}])
    result = asyncio.run(_run_synth(orch, state))
    # The first bad answer is kept when retry fails
    assert result["summary"] == "bad first answer"
