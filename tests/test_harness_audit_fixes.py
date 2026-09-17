"""Regression tests for the harness audit round.

Fixes verified here:

1. Prefetcher._inflight was unbounded — every kick_off leaked one entry
   per plan step that survived until the next ``reset()``. Real
   production leak (one entry per turn per session, never cleared).
   Now: capped at 256 post-drain entries (LRU eviction), timed-out
   entries are dropped immediately.

2. _recent_tool_results (module-level OrderedDict in orchestrator)
   was accessed concurrently from asyncio.gather(_run_step) without
   any lock. CPython's GIL protects individual dict ops but the
   move_to_end + popitem pair is not atomic — concurrent writes
   could corrupt the LRU order. Now wrapped in _dedupe_lock.

3. PTCExecutor.execute used ``return_exceptions=False`` with a comment
   claiming the opposite — one group's unhandled exception would
   cancel siblings and lose already-completed results. Now
   ``return_exceptions=True`` so each wave's results are kept even
   if one group's executor blows up.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from tradingagents.agent_harness.core import orchestrator as orch_mod
from tradingagents.agent_harness.core.prefetch import Prefetcher


# ---------------------------------------------------------------------------
# 1. Prefetcher._inflight leak
# ---------------------------------------------------------------------------
class _SlowTool:
    def __init__(self, name: str, delay: float = 0.05):
        self.name = name
        self.delay = delay

    async def invoke(self, args, context):
        await asyncio.sleep(self.delay)
        return {"echo": self.name, "args": args}


class _FakeRegistry:
    def __init__(self, tools: dict):
        self._tools = tools

    def get(self, name):
        return self._tools[name]


@pytest.mark.asyncio
async def test_prefetch_inflight_is_capped_after_many_turns():
    """After many turn cycles, _inflight must stay bounded.

    Regression for: every kick_off leaked N entries (N = plan steps)
    that survived until explicit reset().
    """
    pf = Prefetcher()
    reg = _FakeRegistry({"get_quote": _SlowTool("get_quote")})
    plan = [{"action": "get_quote", "args": {"symbol": s}} for s in "ABCDE"]
    for _ in range(20):
        pf.kick_off(plan=plan, context=None, tool_registry=reg)
        await pf.drain()
    # Cap is 256 entries. 5 plan steps × 20 turns = 100 < 256, so we
    # expect exactly len(plan) entries to remain (one per unique key).
    assert len(pf._inflight) <= 256
    # And no per-turn growth: the LAST turn's entries survived (no
    # further eviction), but earlier turns' entries were evicted when
    # the cap was approached.
    assert len(pf._inflight) == len(plan)


@pytest.mark.asyncio
async def test_prefetch_inflight_drops_timed_out_entries():
    """Timed-out entries must not pollute the cache."""
    pf = Prefetcher(max_concurrent=1)
    # First tool blocks forever; subsequent kicks must drain with timeout.
    class _Blocking:
        async def invoke(self, args, ctx):
            await asyncio.sleep(10)  # way longer than the drain timeout

    class _Fast:
        async def invoke(self, args, ctx):
            return {"ok": True}

    reg = _FakeRegistry({
        "block": _Blocking(),
        "fast": _Fast(),
    })
    pf.kick_off(plan=[{"action": "block", "args": {}}], context=None, tool_registry=reg)
    # Add a fast tool — should run AFTER the blocking one (max_concurrent=1).
    pf.kick_off(plan=[{"action": "fast", "args": {"x": 1}}], context=None, tool_registry=reg)
    snap = await pf.drain(timeout=0.1)
    assert snap.timed_out >= 1
    # The timed-out entry must NOT be in _inflight (drain evicts).
    assert ("block", frozenset()) not in pf._inflight


@pytest.mark.asyncio
async def test_prefetch_lookup_still_hits_after_drain():
    """The LRU-bounded eviction must not be so aggressive that lookup
    immediately after drain misses."""
    pf = Prefetcher()
    reg = _FakeRegistry({"get_quote": _SlowTool("get_quote")})
    pf.kick_off(
        plan=[{"action": "get_quote", "args": {"symbol": "A"}}],
        context=None, tool_registry=reg,
    )
    await pf.drain()
    hit, result = pf.lookup("get_quote", {"symbol": "A"})
    assert hit is True
    assert result["echo"] == "get_quote"


# ---------------------------------------------------------------------------
# 2. _recent_tool_results thread safety
# ---------------------------------------------------------------------------
def test_dedupe_lookup_record_is_thread_safe():
    """Concurrent _dedupe_record + _dedupe_lookup must not corrupt the
    LRU order or raise. Sanity-check with a tight hammer.
    """
    # Clean slate so we don't collide with concurrent tests
    orch_mod._recent_tool_results.clear()
    errors: list[BaseException] = []

    def worker(session_id: str):
        try:
            for i in range(50):
                orch_mod._dedupe_record(session_id, "get_quote", {"symbol": f"S{i%8}"}, {"price": i})
                orch_mod._dedupe_lookup(session_id, "get_quote", {"symbol": f"S{i%8}"})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"dedupe raised under concurrency: {errors!r}"
    # Cap respected
    assert len(orch_mod._recent_tool_results) <= orch_mod._DEDUPE_MAX_ENTRIES


def test_dedupe_lookup_expired_entry_is_removed():
    """Expired entries are removed (regression for window logic)."""
    orch_mod._recent_tool_results.clear()
    orch_mod._dedupe_record("s1", "get_quote", {"symbol": "X"}, {"price": 1})
    # Manually age the entry past the window
    key = next(iter(orch_mod._recent_tool_results))
    ts, result = orch_mod._recent_tool_results[key]
    orch_mod._recent_tool_results[key] = (ts - orch_mod._DEDUPE_WINDOW_SECONDS - 1, result)
    miss = orch_mod._dedupe_lookup("s1", "get_quote", {"symbol": "X"})
    assert miss is None


# ---------------------------------------------------------------------------
# 3. PTCExecutor.execute failure cascade
# ---------------------------------------------------------------------------
class _FakeSchema:
    args_schema = dict  # PTCExecutor._run_group → tool.schema.args_schema

class _Boom:
    schema = _FakeSchema()
    async def invoke(self, args, ctx):
        raise RuntimeError("intentional crash")


class _Quiet:
    schema = _FakeSchema()
    async def invoke(self, args, ctx):
        return {"ok": True, "args": args}


class _Registry:
    def __init__(self, tools): self._t = tools
    def get(self, name): return self._t[name]


def test_ptc_one_group_failure_does_not_cancel_siblings():
    """Regression: ``return_exceptions=False`` used to lose sibling
    results when one group crashed. Now ``return_exceptions=True``
    keeps the wave's collected results intact."""

    async def _run():
        from tradingagents.agent_harness.ptc import (
            PTCExecutor, PTCGroup, PTCCall, PTCProgram,
        )
        executor = PTCExecutor(tool_registry=_Registry({
            "boom": _Boom(),
            "quiet": _Quiet(),
        }))
        program = PTCProgram(groups=[
            PTCGroup(id="g1", calls=[PTCCall(name="boom", args={})]),
            PTCGroup(id="g2", calls=[PTCCall(name="quiet", args={"x": 1})]),
        ])
        results = await executor.execute(program, context=None)
        return results

    results = asyncio.run(_run())
    # Both groups contributed results (one error, one success).
    by_name = {r["name"]: r for r in results}
    assert "boom" in by_name
    assert by_name["boom"]["ok"] is False
    assert "quiet" in by_name
    assert by_name["quiet"]["ok"] is True


def test_ptc_executor_return_exceptions_does_not_lose_wave_results():
    """Deeper regression: if _run_group itself raises (via a tool
    whose exception escapes _one's ``except Exception`` handler), the
    wave must still produce a synthetic error entry rather than
    propagating up and losing sibling results."""

    async def _run():
        from tradingagents.agent_harness import ptc as ptc_mod

        # Custom BaseException subclass — NOT caught by ``except Exception``
        # inside PTCExecutor._one. _run_group will therefore raise.
        class _Critical(pyc_exc := __import__("builtins").BaseException):
            pass

        class _Catastrophic:
            schema = _FakeSchema()
            async def invoke(self, args, ctx):
                raise _Critical("critical failure inside tool")

        executor = ptc_mod.PTCExecutor(tool_registry=_Registry({
            "crit": _Catastrophic(),
        }))
        program = ptc_mod.PTCProgram(groups=[
            ptc_mod.PTCGroup(id="g1", calls=[ptc_mod.PTCCall(name="crit", args={})]),
        ])
        return await executor.execute(program, context=None)

    results = asyncio.run(_run())
    # With the fix: BaseException is captured into a synthetic error
    # entry — propagated up the wave's gather(return_exceptions=True)
    # but never escapes ``execute``. Without the fix, the BaseException
    # escapes the executor entirely.
    assert results is not None
    assert any(r["ok"] is False for r in results)
