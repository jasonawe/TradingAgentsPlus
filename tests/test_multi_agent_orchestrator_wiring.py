"""Regression tests for the Phase 1 multi_agent flag-gated dispatch.

Rev.8 BLOCKER #3 (Kant round-7) rewrite: previous rev.7 tests used
`from tradingagents.default_config import cfg` (no such symbol), called
`orchestrator._plan(...)` with Python Ellipsis as args (TypeError at
runtime), and built a stub GraphSpec with non-existent `terminals=` /
`metadata=` kwargs. New tests pin the actual contract against the
helper extracted in Task 11.3 (`_maybe_run_multi_agent`).

These tests rely on `tests/conftest.py::_isolate_config` autouse fixture
to reset `dataflows._config` before/after each test. If that fixture is
removed, the cfg.set_config mutations will leak across tests.
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from tradingagents.dataflows.config import set_config
from tradingagents.agent_harness.core import orchestrator as orch_mod
from tradingagents.agent_harness.runtime.multi_agent import CompileError
from tradingagents.agent_harness.runtime.multi_agent.graph import GraphSpec, Edge


def _run(coro):
    """asyncio.run() wrapper — repo convention (no pytest-asyncio)."""
    return asyncio.run(coro)


def _stub_spec():
    """Minimal valid GraphSpec — nodes={'a': ToolNode(...)} + a → b data edge."""
    from tradingagents.agent_harness.runtime.multi_agent.nodes import ToolNode
    n1 = ToolNode(id="a", agent_id="data_agent",
                  tool_name="get_quote", raw_args={"symbol": "X"})
    return GraphSpec(
        nodes={"a": n1},
        edges=[Edge(src="a", dst="a", kind="data")],
        entry="a", exit="a",
    )


async def _call_dispatch(plan):
    """Invoke _maybe_run_multi_agent with a stub orch_state — the helper
    extracted in Task 11.3 (rev.8 BLOCKER #2). Returns whatever the helper
    returns (compiled spec on success, None on fall-through)."""
    fake_state = MagicMock()
    fake_state.session_id = "test-session"
    fake_state.intent = MagicMock()
    fake_state.intent.value = "compare"
    fake_state.turn = MagicMock()
    fake_state.turn.turn_id = "test-turn"
    return await orch_mod._maybe_run_multi_agent(plan, fake_state)


def test_orchestrator_flag_off_skips_multi_agent(monkeypatch):
    """runtime.multi_agent=False → helper returns None without calling
    PlanCompiler.compile or GraphExecutor.run."""
    set_config({"runtime": {"multi_agent": False}})

    compile_called = []
    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.PlanCompiler.compile",
        lambda self, plan: compile_called.append(plan) or _stub_spec(),
    )
    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.GraphExecutor.run",
        AsyncMock(side_effect=AssertionError(
            "GraphExecutor.run must NOT be called when flag is off")),
    )

    result = _run(_call_dispatch(plan=MagicMock()))
    assert result is None, "flag-off must return None (no graph run)"
    assert compile_called == [], "flag-off must skip PlanCompiler"


def test_orchestrator_flag_on_runs_multi_agent(monkeypatch):
    """runtime.multi_agent=True → helper returns compiled spec; PlanCompiler
    invoked once and GraphExecutor.run invoked once."""
    set_config({"runtime": {"multi_agent": True}})

    compile_called = []
    run_called = []

    class _StubCompiler:
        def compile(self, plan):
            compile_called.append(plan)
            return _stub_spec()

    class _StubExecutor:
        def __init__(self, *a, **kw):
            self.kw = kw

        async def run(self, spec, state):
            run_called.append((spec, state))
            return state

    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.PlanCompiler",
        _StubCompiler,
    )
    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.GraphExecutor",
        _StubExecutor,
    )

    result = _run(_call_dispatch(plan=MagicMock()))
    assert len(compile_called) == 1, "flag-on must invoke PlanCompiler once"
    assert len(run_called) == 1, "flag-on must invoke GraphExecutor once"
    assert result is not None, "flag-on success returns the compiled spec"


def test_orchestrator_flag_on_falls_through_on_compile_error(monkeypatch, caplog):
    """runtime.multi_agent=True + PlanCompiler raises CompileError → helper
    returns None (no exception), logs fall-through warning."""
    set_config({"runtime": {"multi_agent": True}})

    class _BoomCompiler:
        def compile(self, plan):
            raise CompileError("boom")

    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.PlanCompiler",
        _BoomCompiler,
    )
    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.GraphExecutor.run",
        AsyncMock(side_effect=AssertionError(
            "executor must not be called when compile fails")),
    )

    caplog.set_level(logging.WARNING)
    result = _run(_call_dispatch(plan=MagicMock()))
    assert result is None, "CompileError must yield None (fall through)"
    assert any(
        "graph compile failed, falling back to PTC" in rec.message
        for rec in caplog.records
    ), "CompileError must log a fall-through warning"


def test_orchestrator_flag_on_falls_through_on_settings_error(monkeypatch, caplog):
    """Rev.7 MEDIUM #2: Pydantic ValidationError from load_settings() must
    also fall through to None (no NameError for `_ForcePTCFallback`)."""
    set_config({"runtime": {"multi_agent": True}})

    def _boom_settings():
        raise ValueError("invalid runtime schema")

    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.load_settings",
        _boom_settings,
    )

    caplog.set_level(logging.WARNING)
    result = _run(_call_dispatch(plan=MagicMock()))
    assert result is None, "settings error must fall through to None"
    assert any(
        "runtime settings invalid, falling back to PTC" in rec.message
        for rec in caplog.records
    )



def test_orchestrator_flag_on_falls_through_on_executor_runtime_error(monkeypatch, caplog):
    """Rev.10 MEDIUM #1: helper's 4th fall-through path — GraphExecutor.run
    raises Exception → helper returns None, logs fall-through warning."""
    set_config({"runtime": {"multi_agent": True}})

    class _OkCompiler:
        def compile(self, plan):
            return _stub_spec()

    class _BoomExecutor:
        def __init__(self, *a, **kw):
            pass

        async def run(self, spec, state):
            raise RuntimeError("executor internal error")

    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.PlanCompiler",
        _OkCompiler,
    )
    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.GraphExecutor",
        _BoomExecutor,
    )

    caplog.set_level(logging.WARNING)
    result = _run(_call_dispatch(plan=MagicMock()))
    assert result is None, "executor errors must fall through to None"
    assert any(
        "graph execution failed, falling back to PTC" in rec.message
        for rec in caplog.records
    ), "executor errors must log a fall-through warning"
