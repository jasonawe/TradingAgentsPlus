"""P5 tests: 6 sub-agents + AgentRegistry + BaseAgent."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.agents import (  # noqa: E402
    AgentContext,
    AgentInput,
    AgentRegistry,
    AgentResult,
    AlphaAgent,
    BaseAgent,
    DataAgent,
    NewsAgent,
    PlannerAgent,
    SynthesizerAgent,
    VerifierAgent,
)


def _ctx() -> AgentContext:
    return AgentContext(session_id="t")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_register_and_get() -> None:
    reg = AgentRegistry()
    a = PlannerAgent()
    reg.register(a)
    assert reg.get("planner") is a
    assert "planner" in reg.list()


def test_registry_rejects_duplicate() -> None:
    reg = AgentRegistry()
    reg.register(PlannerAgent())
    with pytest.raises(ValueError):
        reg.register(PlannerAgent())


def test_registry_get_unknown_raises() -> None:
    reg = AgentRegistry()
    with pytest.raises(KeyError):
        reg.get("nonexistent")


def test_registry_plan_capabilities() -> None:
    reg = AgentRegistry()
    for cls in (PlannerAgent, VerifierAgent, DataAgent, AlphaAgent, NewsAgent, SynthesizerAgent):
        reg.register(cls())
    caps = reg.plan_capabilities()
    assert len(caps) == 6
    assert {c["agent"] for c in caps} == {
        "planner", "verifier", "data_agent", "alpha_agent", "news_agent", "synthesizer",
    }


# ---------------------------------------------------------------------------
# Each sub-agent: name + description + tools + run() success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls,expected_name,expected_tools",
    [
        (PlannerAgent, "planner", []),
        (VerifierAgent, "verifier", []),
        (DataAgent, "data_agent", ["get_quote", "get_quotes_batch", "get_fundamentals"]),
        (AlphaAgent, "alpha_agent", ["list_alpha_factors", "compute_alpha_factors", "evaluate_alpha"]),
        (NewsAgent, "news_agent", ["get_news"]),
        (SynthesizerAgent, "synthesizer", []),
    ],
)
def test_agent_metadata(cls, expected_name, expected_tools) -> None:
    a = cls()
    assert a.name == expected_name
    assert a.description
    assert list(a.tools) == expected_tools
    assert a.timeout_seconds > 0


def test_data_agent_excludes_history() -> None:
    """C1 fix: DataAgent must NOT include get_history."""
    a = DataAgent()
    assert "get_history" not in a.tools


# ---------------------------------------------------------------------------
# Run() smoke tests
# ---------------------------------------------------------------------------


def test_planner_generates_plan_with_symbol() -> None:
    a = PlannerAgent()
    res = asyncio.run(a.run(AgentInput(user_message="分析 600036.SS 估值"), context=_ctx()))
    assert res.success
    plan = res.structured_data["plan"]
    assert any(step.get("agent") == "data_agent" for step in plan)
    assert "600036.SS" in res.structured_data["symbols"]


def test_planner_asks_for_symbol_when_missing() -> None:
    a = PlannerAgent()
    res = asyncio.run(a.run(AgentInput(user_message="hello world"), context=_ctx()))
    assert res.success
    plan = res.structured_data["plan"]
    assert plan[0]["agent"] == "synthesizer"
    assert plan[0]["args"].get("ask_user_for_symbol") is True


def test_verifier_flags_tool_errors() -> None:
    a = VerifierAgent()
    res = asyncio.run(
        a.run(
            AgentInput(
                user_message="x",
                context={"tool_results": [{"error": "boom"}, {"name": "ok", "result": {}}]},
            ),
            context=_ctx(),
        )
    )
    assert res.success is False
    assert "1 tool errors" in res.content


def test_verifier_passes_when_clean() -> None:
    a = VerifierAgent()
    res = asyncio.run(
        a.run(
            AgentInput(
                user_message="x",
                context={"tool_results": [{"name": "get_quote", "result": {"price": 1}}]},
            ),
            context=_ctx(),
        )
    )
    assert res.success is True
    assert "all checks passed" in res.content


def test_data_agent_returns_symbol() -> None:
    a = DataAgent()
    res = asyncio.run(
        a.run(
            AgentInput(user_message="x", context={"symbol": "600036.SS"}),
            context=_ctx(),
        )
    )
    assert res.success
    assert res.structured_data["symbol"] == "600036.SS"


def test_synthesizer_counts_tool_results() -> None:
    a = SynthesizerAgent()
    res = asyncio.run(
        a.run(
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


def test_stream_yields_agent_final() -> None:
    a = PlannerAgent()

    async def _collect():
        out = []
        async for ev in a.stream(AgentInput(user_message="hello"), context=_ctx()):
            out.append(ev)
        return out

    events = asyncio.run(_collect())
    assert any(ev[0] == "agent_final" for ev in events)
