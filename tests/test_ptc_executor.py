"""P0-2 PTC executor tests — concurrent tool calls + dependency waves.

Covers:
- Parse JSON dict → PTCProgram (Pydantic validation)
- Empty / malformed program rejection
- Single group, single call (baseline)
- Single group, 3 concurrent calls (verify asyncio.gather fires in parallel)
- Multi-wave with depends_on (g2 waits for g1)
- Diamond / chain dependencies
- Duplicate group ids / unknown deps / cycles
- Tool not found → graceful error
- Args coerce failure / invoke exception → graceful error
- One failing call doesn't block siblings
"""
from __future__ import annotations

import asyncio
import time

import pytest

from tradingagents.agent_harness.tools.base import BaseTool
from tradingagents.agent_harness.tools.schema import ToolSchema, RetryPolicy
from tradingagents.agent_harness.tools.context import ToolContext
from tradingagents.agent_harness.tools.permission import PermissionType
from tradingagents.agent_harness.ptc import (
    PTCProgram, PTCExecutor, parse_program,
)


# --------------------------------------------------------------------------
# Mock tool registry + tools (avoid touching real tool impls)
# --------------------------------------------------------------------------
class _MockTool(BaseTool):
    """Records concurrent invocations + returns configured result."""

    def __init__(self, name: str, return_value, sleep_ms: int = 0, raise_exc=None):
        self.schema = ToolSchema(
            name=name,
            description=f"mock {name}",
            args_schema=dict,
            result_schema=dict,
            permission=PermissionType.READ,
        )
        self._return = return_value
        self._sleep_ms = sleep_ms
        self._raise = raise_exc
        self.invocations: list[tuple[float, dict]] = []

    @property
    def name(self) -> str:
        return self.schema.name

    async def invoke(self, args, context):
        self.invocations.append((time.monotonic(), args or {}))
        if self._sleep_ms:
            await asyncio.sleep(self._sleep_ms / 1000)
        if self._raise is not None:
            raise self._raise
        return self._return


class _MockRegistry:
    """Minimal duck-typed registry for PTCExecutor (only .get needed)."""

    def __init__(self, tools: dict[str, _MockTool]):
        self._tools = tools
        self.lookup_log: list[str] = []

    def get(self, name: str) -> BaseTool:
        self.lookup_log.append(name)
        if name not in self._tools:
            raise KeyError(f"tool {name!r} not registered")
        return self._tools[name]


def _run(coro):
    """Helper: run an async coroutine in tests (no pytest-asyncio)."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Parsing tests
# --------------------------------------------------------------------------
class TestParse:
    def test_parse_minimal(self):
        prog = parse_program({"mode": "ptc", "groups": []})
        assert prog.mode == "ptc"
        assert prog.groups == []
        assert prog.is_empty()

    def test_parse_one_group_one_call(self):
        prog = parse_program({
            "mode": "ptc",
            "groups": [{
                "id": "g1",
                "calls": [{"name": "get_quote", "args": {"symbol": "600036.SS"}}],
            }],
        })
        assert len(prog.groups) == 1
        assert prog.groups[0].id == "g1"
        assert prog.groups[0].calls[0].name == "get_quote"
        assert not prog.is_empty()

    def test_parse_rejects_wrong_mode(self):
        with pytest.raises(Exception):
            parse_program({"mode": "sequential", "groups": []})

    def test_missing_mode_defaults_to_ptc(self):
        # mode has default 'ptc' for permissive parsing — caller may omit
        prog = parse_program({"groups": []})
        assert prog.mode == "ptc"


# --------------------------------------------------------------------------
# Execution tests
# --------------------------------------------------------------------------
class TestExecute:
    def test_empty_program_returns_empty(self):
        reg = _MockRegistry({})
        results = _run(PTCExecutor(reg).execute(
            parse_program({"mode": "ptc", "groups": []}), ToolContext(session_id="test")))
        assert results == []

    def test_single_call(self):
        tool = _MockTool("get_quote", {"price": 41.71})
        reg = _MockRegistry({"get_quote": tool})
        prog = parse_program({
            "mode": "ptc",
            "groups": [{"id": "g1", "calls": [{"name": "get_quote", "args": {}}]}],
        })
        results = _run(PTCExecutor(reg).execute(prog, ToolContext(session_id="test")))
        assert len(results) == 1
        r = results[0]
        assert r["name"] == "get_quote"
        assert r["ok"] is True
        assert r["result"] == {"price": 41.71}
        assert r["error"] is None

    def test_concurrent_calls_in_one_group_run_in_parallel(self):
        """3 calls × 50ms sleep each → wall time ~50ms (parallel),
        NOT ~150ms (sequential). asyncio.gather is verified."""
        tools = {
            f"tool_{i}": _MockTool(f"tool_{i}", {"id": i}, sleep_ms=50)
            for i in range(3)
        }
        reg = _MockRegistry(tools)
        prog = parse_program({
            "mode": "ptc",
            "groups": [{"id": "g1", "calls": [
                {"name": f"tool_{i}", "args": {}} for i in range(3)
            ]}],
        })
        t0 = time.monotonic()
        results = _run(PTCExecutor(reg).execute(prog, ToolContext(session_id="test")))
        elapsed = time.monotonic() - t0

        assert len(results) == 3
        assert all(r["ok"] for r in results)
        assert elapsed < 0.13, f"expected <130ms (parallel), got {elapsed*1000:.0f}ms"

    def test_dependency_wave_ordering(self):
        """g2 depends_on g1; g2 must NOT start before g1 finishes."""
        tool1 = _MockTool("t1", "r1", sleep_ms=80)
        tool2 = _MockTool("t2", "r2", sleep_ms=10)
        reg = _MockRegistry({"t1": tool1, "t2": tool2})

        prog = parse_program({
            "mode": "ptc",
            "groups": [
                {"id": "g1", "calls": [{"name": "t1", "args": {}}]},
                {"id": "g2", "calls": [{"name": "t2", "args": {}}],
                 "depends_on": ["g1"]},
            ],
        })
        t_start = time.monotonic()
        _run(PTCExecutor(reg).execute(prog, ToolContext(session_id="test")))

        t1_start = tool1.invocations[0][0] - t_start
        t2_start = tool2.invocations[0][0] - t_start
        assert t2_start >= 0.07, f"g2 started too early: {t2_start*1000:.0f}ms"
        assert t1_start < 0.02, f"g1 should start ~immediately: {t1_start*1000:.0f}ms"

    def test_tool_not_found_returns_graceful_error(self):
        reg = _MockRegistry({})
        prog = parse_program({
            "mode": "ptc",
            "groups": [{"id": "g1", "calls": [
                {"name": "missing_tool", "args": {}}
            ]}],
        })
        results = _run(PTCExecutor(reg).execute(prog, ToolContext(session_id="test")))
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert "missing_tool" in results[0]["error"]

    def test_invoke_exception_returns_graceful_error(self):
        tool = _MockTool("boom", None, raise_exc=RuntimeError("kaboom"))
        reg = _MockRegistry({"boom": tool})
        prog = parse_program({
            "mode": "ptc",
            "groups": [{"id": "g1", "calls": [{"name": "boom", "args": {}}]}],
        })
        results = _run(PTCExecutor(reg).execute(prog, ToolContext(session_id="test")))
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert "kaboom" in results[0]["error"]

    def test_one_failing_call_does_not_block_siblings(self):
        """In one group, a failing call shouldn't cancel its siblings."""
        ok_tool = _MockTool("ok", "ok-result", sleep_ms=20)
        bad_tool = _MockTool("bad", None, raise_exc=RuntimeError("nope"))
        reg = _MockRegistry({"ok": ok_tool, "bad": bad_tool})
        prog = parse_program({
            "mode": "ptc",
            "groups": [{"id": "g1", "calls": [
                {"name": "bad", "args": {}},
                {"name": "ok", "args": {}},
            ]}],
        })
        results = _run(PTCExecutor(reg).execute(prog, ToolContext(session_id="test")))
        by_name = {r["name"]: r for r in results}
        assert by_name["bad"]["ok"] is False
        assert by_name["ok"]["ok"] is True
        assert by_name["ok"]["result"] == "ok-result"

    def test_three_waves_chain(self):
        """g1 → g2 → g3 dependency chain runs in 3 separate waves."""
        tools = {f"t{i}": _MockTool(f"t{i}", f"r{i}", sleep_ms=15) for i in range(3)}
        reg = _MockRegistry(tools)
        prog = parse_program({
            "mode": "ptc",
            "groups": [
                {"id": "g1", "calls": [{"name": "t0", "args": {}}]},
                {"id": "g2", "calls": [{"name": "t1", "args": {}}],
                 "depends_on": ["g1"]},
                {"id": "g3", "calls": [{"name": "t2", "args": {}}],
                 "depends_on": ["g2"]},
            ],
        })
        results = _run(PTCExecutor(reg).execute(prog, ToolContext(session_id="test")))
        assert len(results) == 3
        names = [r["name"] for r in results]
        assert names == ["t0", "t1", "t2"]


# --------------------------------------------------------------------------
# Validation tests (program structure)
# --------------------------------------------------------------------------
class TestValidation:
    def test_duplicate_group_ids_raise(self):
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            PTCExecutor(_MockRegistry({}))._topo_waves(parse_program({
                "mode": "ptc",
                "groups": [
                    {"id": "g1", "calls": []},
                    {"id": "g1", "calls": []},
                ],
            }))

    def test_unknown_depends_on_raises(self):
        with pytest.raises(ValueError, match="unknown"):
            PTCExecutor(_MockRegistry({}))._topo_waves(parse_program({
                "mode": "ptc",
                "groups": [
                    {"id": "g1", "calls": [], "depends_on": ["does_not_exist"]},
                ],
            }))

    def test_cycle_raises(self):
        # g1 depends on g2, g2 depends on g1 → cycle
        with pytest.raises(ValueError, match="[Cc]ycle"):
            PTCExecutor(_MockRegistry({}))._topo_waves(parse_program({
                "mode": "ptc",
                "groups": [
                    {"id": "g1", "calls": [], "depends_on": ["g2"]},
                    {"id": "g2", "calls": [], "depends_on": ["g1"]},
                ],
            }))

    def test_diamond_dependency(self):
        """g1 → g2, g1 → g3, g2+g3 → g4: classic diamond,
        schedules as 3 waves (g1) → (g2+g3) → (g4).
        """
        waves = PTCExecutor(_MockRegistry({}))._topo_waves(parse_program({
            "mode": "ptc",
            "groups": [
                {"id": "g1", "calls": []},
                {"id": "g2", "calls": [], "depends_on": ["g1"]},
                {"id": "g3", "calls": [], "depends_on": ["g1"]},
                {"id": "g4", "calls": [], "depends_on": ["g2", "g3"]},
            ],
        }))
        assert len(waves) == 3
        assert [g.id for g in waves[0]] == ["g1"]
        assert sorted(g.id for g in waves[1]) == ["g2", "g3"]
        assert [g.id for g in waves[2]] == ["g4"]
