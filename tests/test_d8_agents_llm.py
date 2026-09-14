"""P8 tests: 6 sub-agents 真接 LLM (v3 spec §4.1 LLM integration).

Each agent has two paths:
- Stub fallback (no LLM): existing behavior preserved.
- LLM path: invoked when ``llm_factory`` + ``tool_registry`` are wired.

We use a mock LLMProvider to exercise the LLM path without an API key.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.agents import (  # noqa: E402
    AgentContext,
    AgentInput,
    AlphaAgent,
    DataAgent,
    NewsAgent,
    PlannerAgent,
    SynthesizerAgent,
)
from tradingagents.agent_harness.llm import LLMResponse  # noqa: E402
from tradingagents.agent_harness.tools import ToolRegistry  # noqa: E402


# ---------------------------------------------------------------------------
# Mock LLM provider + factory (matches test_d8_llm_e2e pattern)
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


def _ctx() -> AgentContext:
    return AgentContext(session_id="t")


# ---------------------------------------------------------------------------
# Fake tool registry with proper Pydantic args_schema
# ---------------------------------------------------------------------------


class _QuoteArgs(BaseModel):
    symbol: str
    asset_type: Literal["stock", "crypto", "fund"] = "stock"


class _QuoteResult(BaseModel):
    symbol: str
    price: Optional[float] = None
    change_pct: Optional[float] = None


class _FundArgs(BaseModel):
    symbol: str


class _FundResult(BaseModel):
    symbol: str
    pe: Optional[float] = None


class _FactorsResult(BaseModel):
    factors: list[str]


class _NewsArgs(BaseModel):
    symbol: str
    days: int = 7


class _NewsResult(BaseModel):
    news: list[dict]


def _make_registry_with_fakes() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.register(name="get_quote", description="fake", args_schema=_QuoteArgs, result_schema=_QuoteResult)
    def _quote(args: _QuoteArgs) -> _QuoteResult:
        return _QuoteResult(symbol=args.symbol, price=99.5, change_pct=1.2)

    @reg.register(name="get_fundamentals", description="fake", args_schema=_FundArgs, result_schema=_FundResult)
    def _fund(args: _FundArgs) -> _FundResult:
        return _FundResult(symbol=args.symbol, pe=7.5)

    @reg.register(name="list_alpha_factors", description="fake", args_schema=type(None), result_schema=_FactorsResult)
    def _factors(args=None) -> _FactorsResult:
        return _FactorsResult(factors=["alpha001", "alpha041", "alpha101", "alpha158"])

    @reg.register(name="get_news", description="fake", args_schema=_NewsArgs, result_schema=_NewsResult)
    def _news(args: _NewsArgs) -> _NewsResult:
        return _NewsResult(news=[{"title": "Strong earnings beat", "sentiment": 0.7}])

    return reg


# ---------------------------------------------------------------------------
# PlannerAgent LLM path
# ---------------------------------------------------------------------------


def test_planner_llm_path_returns_structured_plan() -> None:
    import json as _json
    plan_json = _json.dumps([
        {"step": 1, "agent": "data_agent", "args": {"symbol": "600036.SS"}},
        {"step": 2, "agent": "synthesizer", "args": {}},
    ])
    provider = MockLLMProvider(response_content=plan_json)
    factory = MockLLMFactory(provider)
    agent = PlannerAgent(llm_factory=factory)
    res = asyncio.run(agent.run(AgentInput(user_message="分析 600036.SS 估值"), context=_ctx()))
    assert res.success
    assert res.structured_data["source"] == "llm"
    assert any(step["agent"] == "data_agent" for step in res.structured_data["plan"])
    assert provider.calls, "LLM was not invoked"


def test_planner_falls_back_when_llm_returns_garbage() -> None:
    provider = MockLLMProvider(response_content="Sorry I cannot plan")
    factory = MockLLMFactory(provider)
    agent = PlannerAgent(llm_factory=factory)
    res = asyncio.run(agent.run(AgentInput(user_message="分析 600036.SS"), context=_ctx()))
    assert res.success
    # Heuristic still finds the symbol and plans data_agent.
    assert res.structured_data["source"] == "heuristic"


def test_planner_falls_back_when_llm_raises() -> None:
    provider = MockLLMProvider(response_content="", raise_exc=ConnectionError("net"))
    factory = MockLLMFactory(provider)
    agent = PlannerAgent(llm_factory=factory)
    res = asyncio.run(agent.run(AgentInput(user_message="600036.SS"), context=_ctx()))
    assert res.success
    assert res.structured_data["source"] == "heuristic"


# ---------------------------------------------------------------------------
# DataAgent LLM path
# ---------------------------------------------------------------------------


def test_data_agent_llm_path_invokes_tools_and_summarizes() -> None:
    provider = MockLLMProvider(response_content="招商银行当前价 99.50,涨幅 +1.2%。")
    factory = MockLLMFactory(provider)
    tool_registry = _make_registry_with_fakes()

    agent = DataAgent(llm_factory=factory, tool_registry=tool_registry)
    res = asyncio.run(
        agent.run(
            AgentInput(user_message="x", context={"symbol": "600036.SS"}),
            context=_ctx(),
        )
    )
    assert res.success
    assert res.tool_results, "tool_results should be populated"
    assert any(r["name"] == "get_quote" for r in res.tool_results)
    assert any(r["name"] == "get_fundamentals" for r in res.tool_results)
    assert "99.50" in res.content
    assert provider.calls, "LLM was not invoked"


def test_data_agent_tools_only_when_no_llm() -> None:
    """No LLM → return tool results without summary."""
    tool_registry = _make_registry_with_fakes()
    agent = DataAgent(tool_registry=tool_registry)  # no llm_factory
    res = asyncio.run(
        agent.run(
            AgentInput(user_message="x", context={"symbol": "600036.SS"}),
            context=_ctx(),
        )
    )
    assert res.success
    assert res.tool_results
    assert "600036.SS" in res.content


def test_data_agent_falls_back_to_stub_when_no_tools_no_llm() -> None:
    agent = DataAgent()
    res = asyncio.run(
        agent.run(
            AgentInput(user_message="x", context={"symbol": "600036.SS"}),
            context=_ctx(),
        )
    )
    assert res.success
    assert res.structured_data["symbol"] == "600036.SS"


def test_data_agent_returns_error_when_no_symbol() -> None:
    agent = DataAgent(llm_factory=MockLLMFactory(MockLLMProvider("")))
    res = asyncio.run(agent.run(AgentInput(user_message="hello"), context=_ctx()))
    assert res.success is False
    assert "no symbol" in res.content


# ---------------------------------------------------------------------------
# DataAgent concurrent tool dispatch
# ---------------------------------------------------------------------------


def test_data_agent_invokes_tools_concurrently() -> None:
    """quote + fundamentals have no data dependency — they must run in
    parallel, not serially. With both tools sleeping 150ms, total wall time
    should stay well below the 300ms serial baseline."""
    import time

    reg = ToolRegistry()

    @reg.register(name="get_quote", description="fake", args_schema=_QuoteArgs, result_schema=_QuoteResult)
    async def _quote(args: _QuoteArgs) -> _QuoteResult:
        await asyncio.sleep(0.15)
        return _QuoteResult(symbol=args.symbol, price=99.5, change_pct=1.2)

    @reg.register(name="get_fundamentals", description="fake", args_schema=_FundArgs, result_schema=_FundResult)
    async def _fund(args: _FundArgs) -> _FundResult:
        await asyncio.sleep(0.15)
        return _FundResult(symbol=args.symbol, pe=7.5)

    agent = DataAgent(tool_registry=reg)
    t0 = time.perf_counter()
    res = asyncio.run(
        agent.run(
            AgentInput(user_message="x", context={"symbol": "600036.SS"}),
            context=_ctx(),
        )
    )
    elapsed = time.perf_counter() - t0

    assert res.success
    names = [r["name"] for r in res.tool_results]
    assert names == ["get_quote", "get_fundamentals"], f"order broken: {names}"
    # Serial baseline = 300ms. Allow 220ms — generous slack for asyncio
    # scheduling on a busy CI box. Anything above this means we regressed
    # back to serial awaits.
    assert elapsed < 0.22, f"tools ran serially (elapsed={elapsed:.3f}s)"


def test_data_agent_preserves_order_when_one_tool_fails() -> None:
    """asyncio.gather preserves spec order regardless of resolution order,
    and per-tool failures are swallowed inside ``_call_tool`` (return None).
    Here ``get_fundamentals`` resolves fast + raises, ``get_quote`` is slow
    + succeeds — the survivor must still appear in spec order."""
    reg = ToolRegistry()

    @reg.register(name="get_quote", description="fake", args_schema=_QuoteArgs, result_schema=_QuoteResult)
    async def _quote(args: _QuoteArgs) -> _QuoteResult:
        await asyncio.sleep(0.05)  # slow but succeeds
        return _QuoteResult(symbol=args.symbol, price=99.5, change_pct=1.2)

    @reg.register(name="get_fundamentals", description="fake", args_schema=_FundArgs, result_schema=_FundResult)
    async def _fund(args: _FundArgs) -> _FundResult:
        # Fast but explodes — _call_tool catches and returns None.
        raise RuntimeError("fundamentals provider down")

    agent = DataAgent(tool_registry=reg)
    res = asyncio.run(
        agent.run(
            AgentInput(user_message="x", context={"symbol": "600036.SS"}),
            context=_ctx(),
        )
    )

    assert res.success
    names = [r["name"] for r in res.tool_results]
    assert names == ["get_quote"], f"order broken or failure leaked: {names}"


# ---------------------------------------------------------------------------
# AlphaAgent LLM path
# ---------------------------------------------------------------------------


def test_alpha_agent_llm_path_invokes_list_and_summarizes() -> None:
    provider = MockLLMProvider(response_content="Top 3 predictive factors: alpha041, alpha101, alpha158.")
    factory = MockLLMFactory(provider)
    tool_registry = _make_registry_with_fakes()

    agent = AlphaAgent(llm_factory=factory, tool_registry=tool_registry)
    res = asyncio.run(agent.run(AgentInput(user_message="x"), context=_ctx()))
    assert res.success
    assert res.tool_results
    assert "alpha041" in res.content or "Top 3" in res.content


def test_alpha_agent_stub_when_no_tools_no_llm() -> None:
    agent = AlphaAgent()
    res = asyncio.run(agent.run(AgentInput(user_message="x"), context=_ctx()))
    assert res.success
    assert "compute_alpha_factors" in res.structured_data["capabilities"]


# ---------------------------------------------------------------------------
# NewsAgent LLM path
# ---------------------------------------------------------------------------


def test_news_agent_llm_path_invokes_get_news_and_summarizes() -> None:
    provider = MockLLMProvider(response_content="Sentiment: positive. Themes: strong earnings, dividend.")
    factory = MockLLMFactory(provider)
    tool_registry = _make_registry_with_fakes()

    agent = NewsAgent(llm_factory=factory, tool_registry=tool_registry)
    res = asyncio.run(
        agent.run(
            AgentInput(user_message="x", context={"symbol": "600036.SS"}),
            context=_ctx(),
        )
    )
    assert res.success
    assert res.tool_results
    assert "Sentiment" in res.content or "sentiment" in res.content.lower()


def test_news_agent_stub_when_no_tools_no_llm() -> None:
    agent = NewsAgent()
    res = asyncio.run(
        agent.run(
            AgentInput(user_message="x", context={"symbol": "600036.SS"}),
            context=_ctx(),
        )
    )
    assert res.success
    assert "600036.SS" in res.content


# ---------------------------------------------------------------------------
# SynthesizerAgent LLM path
# ---------------------------------------------------------------------------


def test_synthesizer_llm_path_summarizes_tool_results() -> None:
    provider = MockLLMProvider(response_content="招商银行现价 99.50,涨幅 +1.2%。")
    factory = MockLLMFactory(provider)
    agent = SynthesizerAgent(llm_factory=factory)
    res = asyncio.run(
        agent.run(
            AgentInput(
                user_message="600036.SS 现在多少钱?",
                context={
                    "tool_results": [{"name": "get_quote", "result": {"price": 99.5}}],
                    "symbols": ["600036.SS"],
                },
            ),
            context=_ctx(),
        )
    )
    assert res.success
    assert res.structured_data["source"] == "llm"
    assert "99.50" in res.content
    assert provider.calls, "LLM was not invoked"


def test_synthesizer_falls_back_when_llm_raises() -> None:
    provider = MockLLMProvider(response_content="", raise_exc=ConnectionError())
    factory = MockLLMFactory(provider)
    agent = SynthesizerAgent(llm_factory=factory)
    res = asyncio.run(
        agent.run(
            AgentInput(
                user_message="x",
                context={"tool_results": [{}, {}], "symbols": ["600036.SS"]},
            ),
            context=_ctx(),
        )
    )
    assert res.success
    assert "2 tool_results" in res.content
    assert "600036.SS" in res.content


# ---------------------------------------------------------------------------
# BaseAgent wiring smoke test
# ---------------------------------------------------------------------------


def test_agent_wiring_via_harness() -> None:
    """Harness injects llm_factory + tool_registry into agents at registration."""
    from tradingagents.agent_harness.harness import Harness

    h = Harness()
    for name in ("planner", "data_agent", "alpha_agent", "news_agent", "synthesizer", "verifier"):
        a = h.agent_registry.get(name)
        assert a.llm_factory is h.llm_factory, f"{name} not wired to harness llm_factory"
        assert a.tool_registry is h.tool_registry, f"{name} not wired to harness tool_registry"
