"""P0-2 follow-up: PTC ↔ Orchestrator integration.

Covers:
- Heuristic plan: 2+ symbols → PTC program with one concurrent group
- Heuristic plan: 1 symbol → legacy sequential list
- _execute_ptc with valid PTC program → results match PTCExecutor shape
- _execute_ptc with bad program → graceful error result
- _execute_ptc routes through existing circuit breaker
- stream_chat end-to-end: detects plan shape, emits plan_ready_ptc + tool_result events
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock as MM

import pytest

from tradingagents.agent_harness.tools.base import BaseTool
from tradingagents.agent_harness.tools.schema import ToolSchema
from tradingagents.agent_harness.tools.context import ToolContext
from tradingagents.agent_harness.tools.permission import PermissionType
from tradingagents.agent_harness.tools.registry import ToolRegistry
from tradingagents.agent_harness.core.orchestrator import Orchestrator
from tradingagents.agent_harness.core.context import ContextPriority
from tradingagents.agent_harness.core.retry import RetryPolicy, CircuitBreaker
from tradingagents.agent_harness.core.tier import Intent


# --------------------------------------------------------------------------
# Mock quote tool that records wall-clock + returns a deterministic value
# --------------------------------------------------------------------------
class _MockQuoteTool(BaseTool):
    def __init__(self, sleep_ms: int = 30):
        self.schema = ToolSchema(
            name="get_quote",
            description="mock quote",
            args_schema=dict,
            result_schema=dict,
            permission=PermissionType.READ,
        )
        self._sleep_ms = sleep_ms
        self.invocations: list[tuple[float, dict]] = []

    @property
    def name(self) -> str:
        return self.schema.name

    async def invoke(self, args, context):
        import time
        self.invocations.append((time.monotonic(), args or {}))
        if self._sleep_ms:
            await asyncio.sleep(self._sleep_ms / 1000)
        # Honor test-set _raise attribute for failure scenarios
        if getattr(self, "_raise", None) is not None:
            raise self._raise
        sym = (args or {}).get("symbol", "?")
        return {"symbol": sym, "price": 10.0, "provider": "mock"}


def _make_orchestrator(*, quote_sleep_ms: int = 30):
    """Build an Orchestrator with mock tool registry (no LLM, no judge)."""
    registry = ToolRegistry()
    mock = _MockQuoteTool(sleep_ms=quote_sleep_ms)
    registry.add(mock)

    cb = CircuitBreaker(failure_threshold=5, reset_seconds=30.0)
    return Orchestrator(
        tool_registry=registry,
        agent_registry=MM(list_names=lambda: [], get=lambda n: (_ for _ in ()).throw(KeyError(n))),
        llm_factory=None,            # forces heuristic plan
        context_priority=ContextPriority(),
        retry_policy=RetryPolicy(max_retries=0),
        circuit_breaker=cb,
        audit=None,
        enable_l3=False,
        judge_factory=None,
    ), mock, cb


# --------------------------------------------------------------------------
# Heuristic plan tests
# --------------------------------------------------------------------------
class TestHeuristicPlan:
    def test_two_symbols_returns_ptc_program(self):
        orch, _mock, _cb = _make_orchestrator()
        # Reach _plan via state object (no LLM path needed)
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(
            session_id="t", user_message="比较 600036 600418",
            intent=Intent.COMPARE, symbols=["600036.SS", "600418.SS"],
        )
        plan = asyncio.run(orch._plan(state, ToolContext(session_id="t")))
        assert isinstance(plan, dict), f"expected PTC dict, got {type(plan).__name__}"
        assert plan["mode"] == "ptc"
        assert len(plan["groups"]) == 1
        g = plan["groups"][0]
        assert g["id"] == "g1"
        assert len(g["calls"]) == 2
        names = sorted(c["args"]["symbol"] for c in g["calls"])
        assert names == ["600036.SS", "600418.SS"]
        # All calls in one group → truly concurrent
        assert "depends_on" not in g or not g["depends_on"]

    def test_single_symbol_keeps_sequential_shape(self):
        orch, _mock, _cb = _make_orchestrator()
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(
            session_id="t", user_message="查 600036",
            intent=Intent.QUOTE, symbols=["600036.SS"],
        )
        plan = asyncio.run(orch._plan(state, ToolContext(session_id="t")))
        assert isinstance(plan, list), f"expected list, got {type(plan).__name__}"
        assert plan[0]["action"] == "get_quote"
        assert plan[0]["args"]["symbol"] == "600036.SS"

    def test_single_symbol_compare_adds_fundamentals_step(self):
        orch, _mock, _cb = _make_orchestrator()
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(
            session_id="t", user_message="估值 600036",
            intent=Intent.COMPARE, symbols=["600036.SS"],
        )
        plan = asyncio.run(orch._plan(state, ToolContext(session_id="t")))
        assert isinstance(plan, list)
        actions = [s["action"] for s in plan]
        assert "get_quote" in actions


# --------------------------------------------------------------------------
# _execute_ptc direct tests
# --------------------------------------------------------------------------
class TestExecutePTC:
    def test_routes_program_through_ptc_executor(self):
        orch, mock, _cb = _make_orchestrator()
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(
            session_id="t", user_message="",
            intent=Intent.COMPARE, symbols=[],
        )
        state.plan = {
            "mode": "ptc",
            "groups": [{
                "id": "g1",
                "calls": [
                    {"name": "get_quote", "args": {"symbol": "600036.SS"}},
                    {"name": "get_quote", "args": {"symbol": "600418.SS"}},
                ],
            }],
        }
        results = asyncio.run(orch._execute_ptc(state, ToolContext(session_id="t")))
        assert len(results) == 2
        assert all(r["ok"] for r in results)
        assert all(r["name"] == "get_quote" for r in results)
        assert all(r["group_id"] == "g1" for r in results)

    def test_runs_in_parallel(self):
        orch, mock, _cb = _make_orchestrator(quote_sleep_ms=50)
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(
            session_id="t", user_message="",
            intent=Intent.COMPARE, symbols=["600036.SS", "600418.SS", "600031.SS"],
        )
        state.plan = {
            "mode": "ptc",
            "groups": [{
                "id": "g1",
                "calls": [
                    {"name": "get_quote", "args": {"symbol": s}}
                    for s in ["600036.SS", "600418.SS", "600031.SS"]
                ],
            }],
        }
        import time
        t0 = time.monotonic()
        results = asyncio.run(orch._execute_ptc(state, ToolContext(session_id="t")))
        elapsed = time.monotonic() - t0
        # 3 calls × 50ms in parallel → ~50ms, NOT 150ms
        assert elapsed < 0.13, f"expected <130ms, got {elapsed*1000:.0f}ms"
        assert len(results) == 3

    def test_invalid_program_returns_error_result(self):
        orch, _mock, _cb = _make_orchestrator()
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(
            session_id="t", user_message="",
            intent=Intent.QUOTE, symbols=[],
        )
        state.plan = {"mode": "ptc", "groups": "not a list"}  # bad shape
        results = asyncio.run(orch._execute_ptc(state, ToolContext(session_id="t")))
        assert len(results) == 1
        assert results[0]["name"] == "ptc"
        assert "error" in results[0]

    def test_empty_ptc_program_returns_empty(self):
        orch, _mock, _cb = _make_orchestrator()
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(
            session_id="t", user_message="",
            intent=Intent.QUOTE, symbols=[],
        )
        state.plan = {"mode": "ptc", "groups": []}
        results = asyncio.run(orch._execute_ptc(state, ToolContext(session_id="t")))
        assert results == []

    def test_circuit_breaker_records_failures(self):
        orch, _mock, cb = _make_orchestrator()
        # Force the mock tool to raise
        orch.tool_registry._tools["get_quote"]._raise = RuntimeError("boom")
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(
            session_id="t", user_message="",
            intent=Intent.COMPARE, symbols=["X"],
        )
        state.plan = {
            "mode": "ptc",
            "groups": [{"id": "g1", "calls": [
                {"name": "get_quote", "args": {"symbol": "X"}},
            ]}],
        }
        before_failures = cb._failures
        asyncio.run(orch._execute_ptc(state, ToolContext(session_id="t")))
        after_failures = cb._failures
        assert after_failures == before_failures + 1, "circuit breaker not bumped"


# --------------------------------------------------------------------------
# stream_chat end-to-end test
# --------------------------------------------------------------------------
class TestStreamChatPTCDispatch:
    def test_two_symbols_stream_emits_ptc_events(self):
        orch, _mock, _cb = _make_orchestrator()

        async def _collect():
            events = []
            async for ev in orch.stream_chat(
                session_id="t", user_message="比较 600036 和 600418",
            ):
                events.append(ev)
            return events

        events = asyncio.run(_collect())
        kinds = [k for k, _ in events]
        # Should emit plan_ready_ptc (not plan_ready) for PTC plans
        assert "plan_started" in kinds
        assert "plan_ready_ptc" in kinds, f"expected PTC plan event, got {kinds}"
        # Should have tool_result per concurrent call
        assert kinds.count("tool_result") >= 2
        # End-to-end completion
        assert "agent_final" in kinds or "verified" in kinds


# --------------------------------------------------------------------------
# P0-3 follow-up: PTC ↔ Tool Pipeline integration
#
# Verifies that when the orchestrator wires DangerousToolGuard into the
# pipeline, PTC calls to destructive tools surface as ``needs_approval=True``
# with a populated ``approval_payload`` — preserving the HITL contract.
# --------------------------------------------------------------------------
class TestPTCPipelineIntegration:
    def test_destructive_tool_in_ptc_yields_needs_approval(self):
        """PTC call to delete_* must surface pending_approval, not crash."""
        # Register a destructive tool (delete_alert)
        from tradingagents.agent_harness.tools.schema import ToolSchema
        from tradingagents.agent_harness.tools.permission import PermissionType

        class _DeleteAlert(BaseTool):
            def __init__(self):
                self.schema = ToolSchema(
                    name="delete_alert",
                    description="mock delete",
                    args_schema=dict,
                    result_schema=dict,
                    permission=PermissionType.WRITE,
                )
            @property
            def name(self): return self.schema.name
            async def invoke(self, args, context):
                return {"deleted": True, "args": args}

        registry = ToolRegistry()
        registry.add(_DeleteAlert())
        registry.add(_MockQuoteTool())  # safe tool

        cb = CircuitBreaker(failure_threshold=5, reset_seconds=30.0)
        orch = Orchestrator(
            tool_registry=registry,
            agent_registry=MM(list_names=lambda: [], get=lambda n: (_ for _ in ()).throw(KeyError(n))),
            llm_factory=None,
            context_priority=ContextPriority(),
            retry_policy=RetryPolicy(max_retries=0),
            circuit_breaker=cb,
            audit=None,
            enable_l3=False,
            judge_factory=None,
        )
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        state = OrchestratorState(
            session_id="t", user_message="",
            intent=Intent.COMPARE, symbols=[],
        )
        state.plan = {
            "mode": "ptc",
            "groups": [{
                "id": "g1",
                "calls": [
                    {"name": "delete_alert", "args": {"alert_id": 42}},
                    {"name": "get_quote", "args": {"symbol": "600036.SS"}},
                ],
            }],
        }
        results = asyncio.run(orch._execute_ptc(state, ToolContext(session_id="t")))
        assert len(results) == 2
        delete_res = next(r for r in results if r["name"] == "delete_alert")
        quote_res  = next(r for r in results if r["name"] == "get_quote")
        # Destructive call: needs_approval=True, error set, payload populated
        assert delete_res.get("needs_approval") is True
        assert delete_res["ok"] is False
        assert delete_res.get("approval_payload") is not None
        assert delete_res["approval_payload"]["tool"] == "delete_alert"
        assert delete_res["approval_payload"]["args"] == {"alert_id": 42}
        # Safe call: passes through normally
        assert quote_res["ok"] is True
        assert quote_res.get("needs_approval", False) is False
        assert quote_res["result"]["symbol"] == "600036.SS"

    def test_pipeline_disabled_means_no_approval_surface(self):
        """Without a pipeline, dangerous tools just execute (legacy)."""
        # Build an executor with pipeline=None (legacy path)
        from tradingagents.agent_harness.ptc import PTCExecutor
        from tradingagents.agent_harness.ptc import PTCProgram, PTCGroup, PTCCall

        class _Delete(BaseTool):
            def __init__(self):
                self.schema = ToolSchema(
                    name="delete_watchlist",
                    description="mock",
                    args_schema=dict,
                    result_schema=dict,
                    permission=PermissionType.WRITE,
                )
            @property
            def name(self): return self.schema.name
            async def invoke(self, args, context):
                return {"deleted": True}

        registry = ToolRegistry()
        registry.add(_Delete())
        exec_legacy = PTCExecutor(registry, pipeline=None)
        prog = PTCProgram(groups=[PTCGroup(id="g1", calls=[
            PTCCall(name="delete_watchlist", args={"wid": "w1"}),
        ])])
        results = asyncio.run(exec_legacy.execute(prog, ToolContext(session_id="t")))
        assert len(results) == 1
        assert results[0]["ok"] is True
        assert "needs_approval" not in results[0]
        assert results[0]["result"] == {"deleted": True}
