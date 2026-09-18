"""Step 39 — async RWLock + per-session manager.

Covers:
- Multiple readers run concurrently
- Writer blocks readers + other writers
- Writer starvation prevention
- Per-session isolation
- Backward compatibility with the existing run() shape
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ------------------------------------------------------------------
# Primitive: SessionRWLock
# ------------------------------------------------------------------
def test_multiple_readers_run_concurrently():
    """Three readers should overlap, finishing in ~max(...) not sum(...)."""
    from tradingagents.agent_harness.core.session_rwlock import SessionRWLock

    async def main():
        lock = SessionRWLock()
        timings: list[float] = []

        async def reader(tag: str, sleep: float):
            async with lock.acquire_read():
                t0 = time.monotonic()
                await asyncio.sleep(sleep)
                timings.append((tag, time.monotonic() - t0))

        t0 = time.monotonic()
        await asyncio.gather(reader("a", 0.1), reader("b", 0.1), reader("c", 0.1))
        elapsed = time.monotonic() - t0
        # If they ran concurrently, elapsed should be ~0.1s, not 0.3s.
        assert elapsed < 0.25, f"readers didn't run concurrently: {elapsed:.2f}s"

    asyncio.run(main())


def test_writer_blocks_readers():
    """A held write lock makes subsequent read acquisitions wait."""
    from tradingagents.agent_harness.core.session_rwlock import SessionRWLock

    async def main():
        lock = SessionRWLock()
        order: list[str] = []

        async def writer():
            async with lock.acquire_write():
                order.append("w-start")
                await asyncio.sleep(0.05)
                order.append("w-end")

        async def reader():
            # Reader arrives AFTER writer starts.
            await asyncio.sleep(0.01)
            async with lock.acquire_read():
                order.append("r-go")

        await asyncio.gather(writer(), reader())
        # Writer end must precede reader acquire (no overlap).
        assert order.index("w-end") < order.index("r-go")

    asyncio.run(main())


def test_writer_priority_prevents_starvation():
    """When a writer is waiting, new readers should NOT run first."""
    from tradingagents.agent_harness.core.session_rwlock import SessionRWLock

    async def main():
        lock = SessionRWLock()
        order: list[str] = []

        # Hold a read lock so a writer has to wait.
        async with lock.acquire_read():
            order.append("r1-hold")
            # While r1 holds, schedule a writer + a second reader.
            writer_task = asyncio.create_task(_do_write(lock, order))
            reader_task = asyncio.create_task(_do_read(lock, order, "r2"))
            # Brief pause to let them block
            await asyncio.sleep(0.02)
            order.append("r1-release")
        # r1 releases; writer should run BEFORE r2 (writer priority).
        await asyncio.gather(writer_task, reader_task)
        # Writer completion must precede r2 acquire.
        assert order.index("w-end") < order.index("r2-go"), (
            f"writer starved: order={order}"
        )

    async def _do_write(lock, order):
        async with lock.acquire_write():
            order.append("w-start")
            await asyncio.sleep(0.01)
            order.append("w-end")

    async def _do_read(lock, order, tag):
        async with lock.acquire_read():
            order.append(f"{tag}-go")

    asyncio.run(main())


def test_writer_drains_writer_exclusive():
    """Two writers never overlap (track concurrent inside count)."""
    from tradingagents.agent_harness.core.session_rwlock import SessionRWLock

    async def main():
        lock = SessionRWLock()
        inside = [0]
        peak = [0]

        async def writer():
            async with lock.acquire_write():
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
                await asyncio.sleep(0.02)
                inside[0] -= 1

        await asyncio.gather(writer(), writer())
        assert peak[0] == 1, f"writers overlapped, peak={peak[0]}"


    asyncio.run(main())


# ------------------------------------------------------------------
# Per-session manager: SessionRWLockManager
# ------------------------------------------------------------------
def test_manager_run_drops_busy_on_second_writer():
    """Second concurrent writer gets a 'busy' event, no orchestrator work."""
    from tradingagents.agent_harness.core.session_rwlock_manager import (
        SessionRWLockManager,
    )

    async def main():
        mgr = SessionRWLockManager()
        ran: list[str] = []

        async def producer(tag: str):
            async def _p():
                ran.append(f"{tag}-start")
                await asyncio.sleep(0.05)
                yield ("done", {"tag": tag})
                ran.append(f"{tag}-end")
            async for ev, p in mgr.run("s1", _p):
                pass

        await asyncio.gather(producer("a"), producer("b"))
        # Exactly one producer should have run end-to-end.
        assert ran.count("a-start") + ran.count("b-start") == 1
        assert ran.count("a-end") + ran.count("b-end") == 1

    asyncio.run(main())


def test_manager_read_locks_dont_block_each_other():
    """Two readers on the same session can hold the lock simultaneously."""
    from tradingagents.agent_harness.core.session_rwlock_manager import (
        SessionRWLockManager,
    )

    async def main():
        mgr = SessionRWLockManager()
        t0 = time.monotonic()

        async def reader():
            async with mgr.acquire_read("s2"):
                await asyncio.sleep(0.05)

        await asyncio.gather(reader(), reader())
        elapsed = time.monotonic() - t0
        assert elapsed < 0.09, f"readers serialised unexpectedly: {elapsed:.2f}s"

    asyncio.run(main())


def test_manager_write_blocks_subsequent_reads():
    """A held write lock makes a later read wait until release."""
    from tradingagents.agent_harness.core.session_rwlock_manager import (
        SessionRWLockManager,
    )

    async def main():
        mgr = SessionRWLockManager()
        order: list[str] = []

        async def writer():
            async with mgr.acquire_write("s3"):
                order.append("w-start")
                await asyncio.sleep(0.05)
                order.append("w-end")

        async def reader():
            await asyncio.sleep(0.01)  # ensure writer starts first
            async with mgr.acquire_read("s3"):
                order.append("r-go")

        await asyncio.gather(writer(), reader())
        assert order.index("w-end") < order.index("r-go")

    asyncio.run(main())


def test_per_session_isolation():
    """Different sessions have independent locks."""
    from tradingagents.agent_harness.core.session_rwlock_manager import (
        SessionRWLockManager,
    )

    async def main():
        mgr = SessionRWLockManager()

        async def hold(sid: str):
            async with mgr.acquire_write(sid):
                await asyncio.sleep(0.05)

        t0 = time.monotonic()
        await asyncio.gather(hold("A"), hold("B"))
        elapsed = time.monotonic() - t0
        # Should run in parallel — ~0.05s, not 0.10s.
        assert elapsed < 0.09

    asyncio.run(main())


def test_session_rwlock_introspection():
    """reader_count / writer_active / writers_waiting reflect state."""
    from tradingagents.agent_harness.core.session_rwlock import SessionRWLock

    async def main():
        lock = SessionRWLock()
        assert lock.reader_count == 0
        assert not lock.writer_active
        assert lock.writers_waiting == 0

        async with lock.acquire_read():
            assert lock.reader_count == 1
            assert not lock.writer_active
        assert lock.reader_count == 0

        async with lock.acquire_write():
            assert lock.writer_active
        assert not lock.writer_active

    asyncio.run(main())


def test_tracked_count_and_reset():
    """tracked_count and reset work as documented."""
    from tradingagents.agent_harness.core.session_rwlock_manager import (
        SessionRWLockManager,
    )

    async def main():
        mgr = SessionRWLockManager()
        async with mgr.acquire_read("a"):
            pass
        async with mgr.acquire_read("b"):
            pass
        # 2 sessions tracked (locks not garbage-collected).
        assert mgr.tracked_count() == 2
        mgr.reset()
        assert mgr.tracked_count() == 0

    asyncio.run(main())
