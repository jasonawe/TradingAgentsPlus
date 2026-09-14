"""P8 tests: L3 LLM-judge verification (v3 spec §D6).

Covers:
- LLMJudgeVerdict schema (score / issues / suggestion / reasoning)
- Verifier.verify_l3() — judge parsing, grounded vs ungrounded, fail-open
- Verifier.should_run_l3() — only when tool_results > 4 AND enable_l3=True
- Orchestrator integration: enable_l3=False skips L3, enable_l3=True triggers L3
- VerifierAgent.run() with judge_factory + enable_l3
- HarnessConfig: judge_provider / judge_model / enable_l3
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.config import HarnessConfig  # noqa: E402
from tradingagents.agent_harness.core.tier import Intent
from tradingagents.agent_harness.core import (  # noqa: E402
    CircuitBreaker,
    ContextPriority,
    Orchestrator,
    RetryPolicy,
    VerificationLevel,
    Verifier,
)
from tradingagents.agent_harness.core.orchestrator import OrchestratorState  # noqa: E402
from tradingagents.agent_harness.core.verification import LLMJudgeVerdict  # noqa: E402
from tradingagents.agent_harness.agents import (  # noqa: E402
    AgentContext,
    AgentInput,
    VerifierAgent,
)
from tradingagents.agent_harness.llm import LLMResponse  # noqa: E402
from tradingagents.agent_harness.tools import ToolRegistry, install_builtin_tools  # noqa: E402


# ---------------------------------------------------------------------------
# Mock LLM (used for both main + judge)
# ---------------------------------------------------------------------------


class MockLLMProvider:
    name = "mock"

    def __init__(self, response_content: str = "", raise_exc: Exception | None = None) -> None:
        self.response_content = response_content
        self.raise_exc = raise_exc
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages, *, temperature=0.0, max_tokens=None, stop=None):
        if self.raise_exc is not None:
            raise self.raise_exc
        return LLMResponse(content=self.response_content, provider="mock", model="mock-model")

    def complete_text(self, *, prompt, system=None, temperature=0.0):
        self.calls.append([
            {"role": "system", "content": system or ""},
            {"role": "user", "content": prompt},
        ])
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response_content


class MockLLMFactory:
    def __init__(self, provider: MockLLMProvider) -> None:
        self._provider = provider
        self.default_provider = "mock"
        self.default_model = "mock-model"

    def is_configured(self) -> bool:
        return True

    def make(self, provider=None, model=None, **kwargs):
        return self._provider


# ---------------------------------------------------------------------------
# LLMJudgeVerdict schema
# ---------------------------------------------------------------------------


def test_llm_judge_verdict_valid() -> None:
    v = LLMJudgeVerdict(score=0.85, issues=[], suggestion="ok", reasoning="grounded")
    assert v.grounded is True


def test_llm_judge_verdict_below_threshold_ungrounded() -> None:
    v = LLMJudgeVerdict(score=0.5, issues=["made up number"], suggestion="replan")
    assert v.grounded is False


def test_llm_judge_verdict_score_bounds() -> None:
    with pytest.raises(ValidationError):
        LLMJudgeVerdict(score=1.5)
    with pytest.raises(ValidationError):
        LLMJudgeVerdict(score=-0.1)


# ---------------------------------------------------------------------------
# Verifier.should_run_l3
# ---------------------------------------------------------------------------


def test_should_run_l3_requires_enable_flag() -> None:
    v = Verifier(judge_factory=MockLLMFactory(MockLLMProvider("")))
    assert v.should_run_l3([{}] * 10, enable_l3=False) is False


def test_should_run_l3_requires_more_than_4_results() -> None:
    """N12 fix: tool_results > 4 (strict)."""
    v = Verifier(judge_factory=MockLLMFactory(MockLLMProvider("")))
    # 4 results → not enough
    assert v.should_run_l3([{}] * 4, enable_l3=True) is False
    # 5 results → OK
    assert v.should_run_l3([{}] * 5, enable_l3=True) is True


def test_should_run_l3_requires_judge_factory() -> None:
    v = Verifier()  # no judge_factory
    assert v.should_run_l3([{}] * 10, enable_l3=True) is False


# ---------------------------------------------------------------------------
# Verifier.verify_l3
# ---------------------------------------------------------------------------


def test_verify_l3_grounded_passes() -> None:
    judge = MockLLMProvider(
        response_content=json.dumps({
            "score": 0.9,
            "issues": [],
            "suggestion": "answer is grounded",
            "reasoning": "all numbers match tool outputs",
        })
    )
    v = Verifier(judge_factory=MockLLMFactory(judge))
    result = asyncio.run(
        v.verify_l3(
            user_query="600036.SS 多少钱",
            tool_results=[{"name": "get_quote", "result": {"price": 99.5}}] * 5,
            llm_answer="招商银行当前价 99.50",
        )
    )
    assert result.ok is True
    assert result.level == VerificationLevel.L3_LLM_JUDGE
    assert "grounded" in result.reason
    assert result.details["score"] == 0.9


def test_verify_l3_ungrounded_fails() -> None:
    judge = MockLLMProvider(
        response_content=json.dumps({
            "score": 0.3,
            "issues": ["made up the price 50.00 (tool returned 99.50)"],
            "suggestion": "replan",
            "reasoning": "hallucinated price",
        })
    )
    v = Verifier(judge_factory=MockLLMFactory(judge))
    result = asyncio.run(
        v.verify_l3(
            user_query="600036.SS 多少钱",
            tool_results=[{"name": "get_quote", "result": {"price": 99.5}}] * 5,
            llm_answer="招商银行当前价 50.00",
        )
    )
    assert result.ok is False
    assert result.details["score"] == 0.3


def test_verify_l3_handles_json_in_markdown_fence() -> None:
    judge = MockLLMProvider(
        response_content="```json\n" + json.dumps({
            "score": 0.8,
            "issues": [],
            "suggestion": "ok",
            "reasoning": "grounded",
        }) + "\n```"
    )
    v = Verifier(judge_factory=MockLLMFactory(judge))
    result = asyncio.run(
        v.verify_l3(
            user_query="x",
            tool_results=[{}] * 5,
            llm_answer="x",
        )
    )
    assert result.ok is True


def test_verify_l3_fails_open_on_non_json() -> None:
    judge = MockLLMProvider(response_content="Sorry I cannot judge")
    v = Verifier(judge_factory=MockLLMFactory(judge))
    result = asyncio.run(
        v.verify_l3(user_query="x", tool_results=[{}] * 5, llm_answer="x")
    )
    assert result.ok is True  # fail-open
    assert "non-JSON" in result.reason


def test_verify_l3_fails_open_on_judge_exception() -> None:
    judge = MockLLMProvider(response_content="", raise_exc=ConnectionError("net"))
    v = Verifier(judge_factory=MockLLMFactory(judge))
    result = asyncio.run(
        v.verify_l3(user_query="x", tool_results=[{}] * 5, llm_answer="x")
    )
    assert result.ok is True  # fail-open
    assert "judge exception" in result.reason


def test_verify_l3_skips_when_judge_not_configured() -> None:
    v = Verifier()  # no judge_factory
    result = asyncio.run(
        v.verify_l3(user_query="x", tool_results=[{}] * 5, llm_answer="x")
    )
    assert result.ok is True
    assert "judge factory not configured" in result.reason


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


def _build_orchestrator(judge_provider: MockLLMProvider | None, *, enable_l3: bool) -> Orchestrator:
    reg = ToolRegistry()
    install_builtin_tools(reg)
    judge_factory = MockLLMFactory(judge_provider) if judge_provider is not None else None
    return Orchestrator(
        tool_registry=reg,
        agent_registry=_StubRegistry(),
        llm_factory=MockLLMFactory(MockLLMProvider("")),
        context_priority=ContextPriority(),
        retry_policy=RetryPolicy(max_retries=1, backoff_seconds=0),
        circuit_breaker=CircuitBreaker(failure_threshold=10, reset_seconds=30),
        audit=None,
        enable_l3=enable_l3,
        judge_factory=judge_factory,
    )


class _StubRegistry:
    def list(self):
        return ["data_agent", "synthesizer"]

    def get(self, name):
        from types import SimpleNamespace
        return SimpleNamespace(name=name, description=f"handles {name}")


def test_orchestrator_skips_l3_when_disabled() -> None:
    judge = MockLLMProvider(
        response_content=json.dumps({"score": 0.9, "issues": [], "suggestion": "", "reasoning": ""})
    )
    orch = _build_orchestrator(judge, enable_l3=False)
    state = OrchestratorState(
        session_id="t",
        user_message="600036.SS",
        symbols=["600036.SS"],
        tool_results=[{"name": "get_quote"}] * 10,  # > 4
        final={"summary": "招商银行 99.50"},
    )
    result = asyncio.run(orch._verify(state))
    # Should NOT have called the judge
    assert judge.calls == []


def test_orchestrator_runs_l3_when_enabled_and_enough_results() -> None:
    """L3 runs in `_run_l3_judge` AFTER synthesize (so the judge sees the
    LLM answer). Verify the post-synthesize `answer_verified` event."""
    judge = MockLLMProvider(
        response_content=json.dumps({"score": 0.9, "issues": [], "suggestion": "", "reasoning": "grounded"})
    )
    orch = _build_orchestrator(judge, enable_l3=True)
    tool_results = [{"name": "get_quote", "result": {"price": 99.5}}] * 10

    async def fake_execute(state, context):
        return tool_results
    async def fake_synth(state):
        return {"summary": "招商银行 99.50"}
    async def fake_plan(state, context):
        return [{"step": 1, "action": "noop"}]

    orch._execute = fake_execute  # type: ignore[assignment]
    orch._synthesize = fake_synth  # type: ignore[assignment]
    orch._plan = fake_plan  # type: ignore[assignment]

    state = OrchestratorState(
        session_id="t",
        user_message="600036.SS",
        symbols=["600036.SS"],
        intent=Intent.QUOTE,
    )

    async def drive() -> list:
        plan = await orch._plan(state, context=None)
        state.plan = plan
        state.tool_results = await orch._execute(state, context=None)
        await orch._verify(state)
        state.final = await orch._synthesize(state)
        events = []
        async for ev in orch._run_l3_judge(state):
            events.append(ev)
        return events

    events = asyncio.run(drive())
    assert judge.calls, "judge was not invoked"
    answer_verified = next(e for e in events if e[0] == "answer_verified")
    _, payload = answer_verified
    assert payload["level"] == int(VerificationLevel.L3_LLM_JUDGE)
    assert payload["ok"] is True


def test_orchestrator_l3_ungrounded_replans() -> None:
    """L3 ungrounded verdict appends a replan entry to state.plan (spec §D6)."""
    judge = MockLLMProvider(
        response_content=json.dumps({"score": 0.3, "issues": ["hallucinated"], "suggestion": "replan", "reasoning": "bad"})
    )
    orch = _build_orchestrator(judge, enable_l3=True)
    initial_plan = [{"step": 1, "action": "noop"}]
    tool_results = [{"name": "get_quote", "result": {"price": 99.5}}] * 10

    async def fake_execute(state, context):
        return tool_results
    async def fake_synth(state):
        return {"summary": "招商银行 50.00 (made up)"}
    async def fake_plan(state, context):
        return list(initial_plan)
    orch._execute = fake_execute  # type: ignore[assignment]
    orch._synthesize = fake_synth  # type: ignore[assignment]
    orch._plan = fake_plan  # type: ignore[assignment]

    state = OrchestratorState(
        session_id="t",
        user_message="600036.SS",
        symbols=["600036.SS"],
        intent=Intent.QUOTE,
    )

    async def drive() -> None:
        state.plan = await orch._plan(state, context=None)
        state.tool_results = await orch._execute(state, context=None)
        await orch._verify(state)
        state.final = await orch._synthesize(state)
        async for _ in orch._run_l3_judge(state):
            pass

    asyncio.run(drive())
    # The ungrounded verdict appends a replan entry to state.plan.
    assert any("replan_reason" in step.get("args", {}) for step in state.plan)


# ---------------------------------------------------------------------------
# VerifierAgent L3 mode
# ---------------------------------------------------------------------------


def test_verifier_agent_l1_l2_only_by_default() -> None:
    a = VerifierAgent()  # enable_l3 defaults to False
    res = asyncio.run(
        a.run(
            AgentInput(
                user_message="x",
                context={"tool_results": [{}] * 10},  # > 4
            ),
            context=AgentContext(session_id="t"),
        )
    )
    assert res.success is True
    assert "l3_verdict" not in res.structured_data


def test_verifier_agent_l3_when_enabled_and_enough_results() -> None:
    judge = MockLLMProvider(
        response_content=json.dumps({"score": 0.9, "issues": [], "suggestion": "", "reasoning": "grounded"})
    )
    a = VerifierAgent(judge_factory=MockLLMFactory(judge), enable_l3=True)
    res = asyncio.run(
        a.run(
            AgentInput(
                user_message="x",
                context={"tool_results": [{}] * 5},
            ),
            context=AgentContext(session_id="t"),
        )
    )
    assert res.success is True
    assert "l3_verdict" in res.structured_data
    assert judge.calls


def test_verifier_agent_l3_skipped_when_tool_results_le_4() -> None:
    """N12 fix: only run L3 when tool_results > 4."""
    judge = MockLLMProvider(response_content=json.dumps({"score": 0.9, "issues": [], "suggestion": "", "reasoning": "x"}))
    a = VerifierAgent(judge_factory=MockLLMFactory(judge), enable_l3=True)
    res = asyncio.run(
        a.run(
            AgentInput(user_message="x", context={"tool_results": [{}, {}]}),
            context=AgentContext(session_id="t"),
        )
    )
    assert res.success is True
    assert "l3_verdict" not in res.structured_data
    assert judge.calls == []


def test_verifier_agent_l3_ungrounded_marks_failure() -> None:
    judge = MockLLMProvider(
        response_content=json.dumps({"score": 0.2, "issues": ["hallucinated"], "suggestion": "replan", "reasoning": "bad"})
    )
    a = VerifierAgent(judge_factory=MockLLMFactory(judge), enable_l3=True)
    res = asyncio.run(
        a.run(
            AgentInput(user_message="x", context={"tool_results": [{}] * 5}),
            context=AgentContext(session_id="t"),
        )
    )
    assert res.success is False
    assert "L3 ungrounded" in res.content


# ---------------------------------------------------------------------------
# HarnessConfig integration
# ---------------------------------------------------------------------------


def test_harness_config_judge_fields_present() -> None:
    cfg = HarnessConfig.from_env()
    assert hasattr(cfg, "judge_provider")
    assert hasattr(cfg, "judge_model")
    assert hasattr(cfg, "enable_l3")


def test_harness_wires_judge_factory_from_config(monkeypatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_ENABLE_L3", "1")
    monkeypatch.setenv("TRADINGAGENTS_JUDGE_PROVIDER", "judge-prov")
    monkeypatch.setenv("TRADINGAGENTS_JUDGE_MODEL", "judge-model-x")
    cfg = HarnessConfig.from_env()
    assert cfg.enable_l3 is True
    assert cfg.judge_provider == "judge-prov"
    assert cfg.judge_model == "judge-model-x"

    # And the harness should pass these through to the orchestrator + verifier.
    from tradingagents.agent_harness.harness import Harness
    h = Harness(config=cfg)
    assert h.orchestrator.enable_l3 is True
    assert h.judge_factory is not h.llm_factory
    assert h.agent_registry.get("verifier").enable_l3 is True


def test_harness_judge_factory_falls_back_to_main_llm() -> None:
    """When judge_provider/model are empty, judge_factory == llm_factory."""
    from tradingagents.agent_harness.harness import Harness
    h = Harness()
    assert h.judge_factory is h.llm_factory
