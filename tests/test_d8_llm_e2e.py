"""End-to-end LLM path tests for Orchestrator._llm_plan + _llm_synthesize.

Uses a mock LLMProvider to exercise the LLM path without an API key.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core import (  # noqa: E402
    CircuitBreaker,
    ContextPriority,
    Orchestrator,
    RetryPolicy,
)
from tradingagents.agent_harness.core.orchestrator import OrchestratorState  # noqa: E402
from tradingagents.agent_harness.llm import LLMResponse  # noqa: E402
from tradingagents.agent_harness.tools import ToolRegistry, install_builtin_tools  # noqa: E402


class MockLLMProvider:
    name = "mock"

    def __init__(self, response_content: str = "", raise_exc: Exception | None = None) -> None:
        self.response_content = response_content
        self.raise_exc = raise_exc
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages, *, temperature=0.0, max_tokens=None, stop=None):
        if self.raise_exc is not None:
            raise self.raise_exc
        msgs = [{"role": m.role, "content": m.content} for m in messages]
        self.calls.append(msgs)
        return LLMResponse(content=self.response_content, provider="mock", model="mock-model")

    def complete_text(self, *, prompt, system=None, temperature=0.0):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append([
            {"role": "system", "content": system or ""},
            {"role": "user", "content": prompt},
        ])
        return self.response_content  # base.ABC convention: return str


class MockLLMFactory:
    def __init__(self, provider: MockLLMProvider) -> None:
        self._provider = provider
        self.default_provider = "mock"
        self.default_model = "mock-model"

    def is_configured(self) -> bool:
        return True

    def make(self, provider=None, model=None, **kwargs):
        return self._provider


def _build_orchestrator(mock_provider: MockLLMProvider) -> Orchestrator:
    reg = ToolRegistry()
    install_builtin_tools(reg)

    class _RegistryStub:
        def list(self):
            return ["data_agent", "alpha_agent", "synthesizer"]

        def get(self, name):
            return SimpleNamespace(name=name, description=f"handles {name} queries")

    return Orchestrator(
        tool_registry=reg,
        agent_registry=_RegistryStub(),
        llm_factory=MockLLMFactory(mock_provider),
        context_priority=ContextPriority(),
        retry_policy=RetryPolicy(max_retries=1, backoff_seconds=0),
        circuit_breaker=CircuitBreaker(failure_threshold=10, reset_seconds=30),
        audit=None,
    )


# ===========================================================================
# _llm_plan end-to-end
# ===========================================================================


def test_llm_plan_called_when_factory_configured() -> None:
    plan_json = json.dumps([
        {"step": 1, "agent": "data_agent", "args": {"symbol": "600036.SS"}},
        {"step": 2, "agent": "synthesizer", "args": {}},
    ])
    provider = MockLLMProvider(response_content=plan_json)
    orch = _build_orchestrator(provider)
    state = OrchestratorState(
        session_id="t", user_message="分析 600036.SS 估值",
        symbols=["600036.SS"],
    )
    plan = asyncio.run(orch._llm_plan(state))
    assert len(plan) == 2
    assert plan[0]["agent"] == "data_agent"
    assert provider.calls, "LLM was not called"


def test_llm_plan_handles_json_in_markdown_fence() -> None:
    plan_json = "```json\n" + json.dumps([
        {"step": 1, "agent": "data_agent", "args": {"symbol": "AAPL"}},
    ]) + "\n```"
    provider = MockLLMProvider(response_content=plan_json)
    orch = _build_orchestrator(provider)
    state = OrchestratorState(session_id="t", user_message="AAPL", symbols=["AAPL"])
    plan = asyncio.run(orch._llm_plan(state))
    assert len(plan) == 1
    assert plan[0]["args"]["symbol"] == "AAPL"


def test_llm_plan_falls_back_when_llm_raises() -> None:
    provider = MockLLMProvider(response_content="", raise_exc=RuntimeError("API down"))
    orch = _build_orchestrator(provider)
    state = OrchestratorState(session_id="t", user_message="600036.SS", symbols=["600036.SS"])
    plan = asyncio.run(orch._llm_plan(state))
    assert plan == []


def test_llm_plan_falls_back_when_response_not_json() -> None:
    provider = MockLLMProvider(response_content="Sorry, I cannot help with that.")
    orch = _build_orchestrator(provider)
    state = OrchestratorState(session_id="t", user_message="600036.SS", symbols=["600036.SS"])
    plan = asyncio.run(orch._llm_plan(state))
    assert plan == []


# ===========================================================================
# _llm_synthesize end-to-end
# ===========================================================================


def test_llm_synthesize_calls_llm_and_returns_summary() -> None:
    provider = MockLLMProvider(response_content="招商银行当前价 99.50,涨幅 +1.2%")
    orch = _build_orchestrator(provider)
    state = OrchestratorState(
        session_id="t", user_message="600036.SS 多少钱",
        symbols=["600036.SS"],
        tool_results=[{"name": "get_quote", "result": {"price": 99.5}}],
    )
    result = asyncio.run(orch._llm_synthesize(state))
    assert "summary" in result
    assert "99.50" in result["summary"]
    assert provider.calls, "LLM was not called"


def test_llm_synthesize_falls_back_on_failure() -> None:
    provider = MockLLMProvider(response_content="", raise_exc=ConnectionError("network down"))
    orch = _build_orchestrator(provider)
    state = OrchestratorState(
        session_id="t", user_message="600036.SS 多少钱",
        symbols=["600036.SS"],
        tool_results=[{"name": "get_quote"}],
    )
    result = asyncio.run(orch._llm_synthesize(state))
    assert "summary" in result
    assert "failed" in result["summary"].lower() or "not configured" in result["summary"].lower()
