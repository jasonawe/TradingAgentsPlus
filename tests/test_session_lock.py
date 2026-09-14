"""SessionLockManager — per-session in-flight lock for stream_chat.

P1 priority item: prevents concurrent stream_chat for the same
session_id from racing on circuit breaker / event emission / audit log.
"""
from __future__ import annotations

import asyncio
import pytest

from tradingagents.agent_harness.core.session_lock import SessionLockManager


def _run(coro):
    """Run an async coroutine in tests (no pytest-asyncio)."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Basic behaviour
# --------------------------------------------------------------------------
class TestBasicLock:
    def test_run_idle_session_proceeds(self):
        mgr = SessionLockManager()
        events = []

        async def _producer():
            yield ("ok", {"v": 1})

        async def _collect():
            async for ev, p in mgr.run("s1", _producer):
                events.append((ev, p))

        _run(_collect())
        assert events == [("ok", {"v": 1})]

    def test_second_concurrent_call_yields_busy(self):
        mgr = SessionLockManager()
        # First request holds the lock for a while; second must yield busy.
        async def _slow_producer():
            await asyncio.sleep(0.05)
            yield ("ok", {"v": 1})

        async def _second():
            async for ev, p in mgr.run("s1", _slow_producer):
                pass

        async def _first():
            async for ev, p in mgr.run("s1", _slow_producer):
                pass

        async def _orchestrate():
            t1 = asyncio.create_task(_first())
            await asyncio.sleep(0.01)  # let first grab the lock
            t2 = asyncio.create_task(_second())
            await asyncio.gather(t1, t2)

        busy_events = []

        async def _watch_second():
            async for ev, p in mgr.run("s1", _slow_producer):
                if ev == "busy":
                    busy_events.append(p)

        async def _all():
            t1 = asyncio.create_task(_first())
            await asyncio.sleep(0.01)
            t2 = asyncio.create_task(_watch_second())
            await asyncio.gather(t1, t2)

        _run(_all())
        assert len(busy_events) == 1, "expected exactly one busy event"
        ev = busy_events[0]
        assert ev["session_id"] == "s1"
        assert ev["active_run_id"] is not None
        assert "in-flight" in ev["message"]

    def test_different_sessions_do_not_block(self):
        mgr = SessionLockManager()
        finished: list[str] = []

        async def _slow(sid):
            await asyncio.sleep(0.02)
            yield ("ok", {"sid": sid})

        async def _runner(sid):
            async for ev, p in mgr.run(sid, lambda: _slow(sid)):
                finished.append(p.get("sid"))

        async def _orchestrate():
            await asyncio.gather(_runner("a"), _runner("b"), _runner("c"))

        _run(_orchestrate())
        assert sorted(finished) == ["a", "b", "c"]


# --------------------------------------------------------------------------
# Lock release — second call after first finishes must succeed
# --------------------------------------------------------------------------
class TestRelease:
    def test_lock_released_after_completion(self):
        mgr = SessionLockManager()

        async def _noop_producer():
            return
            yield  # noqa: make this a generator

        async def _first():
            async for _ in mgr.run("s1", _noop_producer):
                pass

        async def _second():
            async for ev, _ in mgr.run("s1", _noop_producer):
                # If lock weren't released we'd see "busy" instead
                return

        async def _orchestrate():
            await _first()
            assert not mgr.is_busy("s1")
            await _second()

        _run(_orchestrate())
        assert not mgr.is_busy("s1")

    def test_lock_released_after_exception(self):
        mgr = SessionLockManager()

        async def _boom():
            raise RuntimeError("orchestrator crash")
            yield  # noqa

        async def _runner():
            try:
                async for _ in mgr.run("s1", _boom):
                    pass
            except RuntimeError:
                pass

        async def _orchestrate():
            await _runner()
            assert not mgr.is_busy("s1")

        _run(_orchestrate())


# --------------------------------------------------------------------------
# Introspection helpers
# --------------------------------------------------------------------------
class TestIntrospection:
    def test_is_busy_true_while_held(self):
        mgr = SessionLockManager()
        result = {}

        async def _slow():
            await asyncio.sleep(0.05)
            result["busy_during"] = mgr.is_busy("s1")
            yield ("ok", {})

        async def _runner():
            async for _ in mgr.run("s1", _slow):
                pass

        async def _orchestrate():
            t = asyncio.create_task(_runner())
            await asyncio.sleep(0.01)  # let lock be acquired
            result["busy_before"] = mgr.is_busy("s1")
            await t
            result["busy_after"] = mgr.is_busy("s1")

        _run(_orchestrate())
        assert result["busy_before"] is True
        assert result["busy_during"] is True
        assert result["busy_after"] is False

    def test_active_run_id_round_trip(self):
        mgr = SessionLockManager()
        captured = {}

        async def _slow():
            await asyncio.sleep(0.05)
            captured["rid"] = mgr.active_run_id("s1")
            yield ("ok", {})

        async def _runner():
            async for _ in mgr.run("s1", _slow):
                pass

        async def _orchestrate():
            t = asyncio.create_task(_runner())
            await asyncio.sleep(0.01)
            captured["rid_outer"] = mgr.active_run_id("s1")
            await t
            captured["rid_after"] = mgr.active_run_id("s1")

        _run(_orchestrate())
        assert captured["rid_outer"] is not None
        assert captured["rid_outer"] == captured["rid"]
        assert captured["rid_after"] is None

    def test_tracked_count_grows_with_sessions(self):
        mgr = SessionLockManager()
        assert mgr.tracked_count() == 0

        async def _noop():
            return
            yield

        async def _runner(sid):
            async for _ in mgr.run(sid, _noop):
                pass

        async def _orchestrate():
            for i in range(5):
                await _runner(f"s{i}")

        _run(_orchestrate())
        assert mgr.tracked_count() == 5


# --------------------------------------------------------------------------
# Eviction — soft cap protection
# --------------------------------------------------------------------------
class TestEviction:
    def test_eviction_when_over_cap(self):
        mgr = SessionLockManager(max_sessions=10)
        # Pre-populate with 12 idle entries to exceed cap
        for i in range(12):
            mgr._locks[f"s{i}"] = type("E", (), {
                "lock": asyncio.Lock(), "run_id": None,
                "locked": lambda self: False,
            })()

        async def _noop():
            return
            yield

        async def _trigger():
            # Triggering one more entry should evict some idle ones
            async for _ in mgr.run("new", _noop):
                pass

        _run(_trigger())
        # Cap behaviour: should have shrunk (or grown, but not unbounded).
        # We don't pin the exact count because it depends on eviction policy,
        # but it must be < 13.
        assert mgr.tracked_count() < 13
