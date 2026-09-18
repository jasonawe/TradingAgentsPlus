"""Step 43 — pull-eval background queue."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_submit_runs_coro_and_emits_events():
    """submit() runs the coro; events surface via drain()."""
    from tradingagents.agent_harness.core.background_queue import (
        BackgroundTaskQueue,
    )

    async def main():
        q = BackgroundTaskQueue()

        async def producer():
            yield ("live_1", {"v": 1})
            await asyncio.sleep(0.02)
            yield ("live_2", {"v": 2})

        q.submit("s1", producer)
        events = []
        async for ev, p in q.drain("s1"):
            events.append((ev, p))
        assert events == [("live_1", {"v": 1}), ("live_2", {"v": 2})]

    asyncio.run(main())


def test_multiple_submissions_are_fifo():
    """Two submissions to the same session drain in submission order."""
    from tradingagents.agent_harness.core.background_queue import (
        BackgroundTaskQueue,
    )

    async def main():
        q = BackgroundTaskQueue()

        async def p1():
            await asyncio.sleep(0)
            yield ("a", {})
        async def p2():
            await asyncio.sleep(0)
            yield ("b", {})

        q.submit("s", p1)
        q.submit("s", p2)
        events = []
        async for ev, _ in q.drain("s"):
            events.append(ev)
        assert events == ["a", "b"]

    asyncio.run(main())


def test_drain_terminates_when_no_in_flight():
    """drain() returns promptly when no tasks are running."""
    from tradingagents.agent_harness.core.background_queue import (
        BackgroundTaskQueue,
    )

    async def main():
        q = BackgroundTaskQueue()
        events = []
        t0 = time.monotonic()
        async for ev, p in q.drain("empty-session"):
            events.append((ev, p))
        elapsed = time.monotonic() - t0
        # Should be near-instant (no timeout).
        assert elapsed < 0.5, f"drain took too long for empty queue: {elapsed}"
        assert events == []

    asyncio.run(main())


def test_drain_surfaces_background_error():
    """A raising coro emits a background_error event."""
    from tradingagents.agent_harness.core.background_queue import (
        BackgroundTaskQueue,
    )

    async def main():
        q = BackgroundTaskQueue()

        async def bad():
            yield ("ok", {})
            raise RuntimeError("boom")

        q.submit("s", bad)
        events = []
        async for ev, p in q.drain("s"):
            events.append((ev, p))
        # First event is the yielded 'ok', second is the caught error.
        assert events[0] == ("ok", {})
        assert events[1][0] == "background_error"
        assert "boom" in events[1][1]["error"]

    asyncio.run(main())


def test_wait_all_returns_zero_when_drained():
    """wait_all() returns 0 once all tasks complete."""
    from tradingagents.agent_harness.core.background_queue import (
        BackgroundTaskQueue,
    )

    async def main():
        q = BackgroundTaskQueue()

        async def short():
            yield ("x", {})

        q.submit("s", short)
        pending = await q.wait_all("s", timeout=1.0)
        assert pending == 0

    asyncio.run(main())


def test_pull_eval_does_not_block_submitter():
    """submit() returns synchronously while coro runs in background."""
    from tradingagents.agent_harness.core.background_queue import (
        BackgroundTaskQueue,
    )

    async def main():
        q = BackgroundTaskQueue()

        async def slow():
            await asyncio.sleep(0.5)
            yield ("slow", {})

        t0 = time.monotonic()
        bt = q.submit("s", slow)
        elapsed = time.monotonic() - t0
        # submit() should return immediately, not wait for slow().
        assert elapsed < 0.05, f"submit blocked: {elapsed:.2f}s"
        # Wait for completion so the test doesn't leak tasks.
        await q.wait_all("s")
        assert bt.task.done()

    asyncio.run(main())


def test_per_session_isolation():
    """Background tasks in session A don't leak into session B's queue."""
    from tradingagents.agent_harness.core.background_queue import (
        BackgroundTaskQueue,
    )

    async def main():
        q = BackgroundTaskQueue()

        async def pa_coro():
            yield ("a_only", {})
        async def pb_coro():
            # Yield twice with a sleep between so the task stays
            # in-flight while we drain A.
            yield ("b_first", {})
            await asyncio.sleep(0.5)
            yield ("b_second", {})

        q.submit("A", pa_coro)
        q.submit("B", pb_coro)
        # Drain A first — short-lived task.
        a_events = []
        async for ev, _ in q.drain("A"):
            a_events.append(ev)
        assert a_events == ["a_only"]
        # B is still in-flight (sleeping between yields).
        assert q.in_flight_count("B") == 1
        # Drain B (will pick up b_first, then sleep, then b_second).
        b_events = []
        async for ev, _ in q.drain("B"):
            b_events.append(ev)
        assert b_events == ["b_first", "b_second"]
        assert q.in_flight_count("B") == 0

    asyncio.run(main())


def test_introspection_helpers():
    """tracked_count + in_flight_count + queued_event_count work."""
    from tradingagents.agent_harness.core.background_queue import (
        BackgroundTaskQueue,
    )

    async def main():
        q = BackgroundTaskQueue()

        async def coro():
            yield ("x", {})

        assert q.tracked_count() == 0
        q.submit("s1", coro)
        assert q.tracked_count() == 1
        assert q.in_flight_count("s1") == 1
        await q.wait_all("s1")
        assert q.in_flight_count("s1") == 0

    asyncio.run(main())
