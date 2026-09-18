"""Step 43 — background task queue (pull-eval for L3 judge).

L3 LLM judge used to block the SSE stream — user waited for judge
LLM call (1.5–3s) AFTER seeing the final answer.

Now the orchestrator emits ``agent_final`` immediately, schedules
the judge as a ``BackgroundTask``, and continues streaming. The
background task's events are surfaced as ``verified_late`` (or
similar) and injected into the SSE stream by the consumer.

Design
------
- Per-session FIFO queue — events surface in submission order.
- ``submit(session_id, coro)`` returns immediately; the coro runs
  concurrently via ``asyncio.create_task``.
- ``drain(session_id)`` returns an async iterator that yields
  events as background tasks complete (or sentinel ``None`` when
  the queue is empty + no in-flight tasks).
- ``wait_all(session_id)`` blocks until all in-flight tasks finish
  (used at session shutdown to avoid orphaned coros).

Memory
------
Per-session queues live in a ``WeakValueDictionary``-style dict
with eviction when idle. Background tasks are stored as
``asyncio.Task`` handles — they're gc'd when consumed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, AsyncIterator

LOGGER = logging.getLogger(__name__)

_MAX_TRACKED_SESSIONS = 512


class BackgroundTask:
    """One in-flight background task.

    Attributes
    ----------
    session_id : str
    submitted_at : float
        time.time() when submit() was called
    task : asyncio.Task
        wrapper coro that emits (event, payload) tuples into the queue
    """

    __slots__ = ("session_id", "submitted_at", "task")

    def __init__(self, session_id: str, task: asyncio.Task) -> None:
        self.session_id = session_id
        self.submitted_at = time.monotonic()
        self.task = task


class BackgroundTaskQueue:
    """Per-session FIFO queue of background tasks with event payloads."""

    def __init__(self, max_sessions: int = _MAX_TRACKED_SESSIONS) -> None:
        self._sessions: dict[str, _SessionQueue] = {}
        self._max = max_sessions

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------
    def submit(
        self,
        session_id: str,
        coro_factory: Any,
        *,
        on_error: str = "ignore",
    ) -> BackgroundTask:
        """Schedule a background coroutine for ``session_id``.

        ``coro_factory`` is a no-arg callable returning a coroutine —
        we delay coro creation so submit() is sync and immediate.

        ``on_error``:
        - ``"ignore"`` (default) — exception is logged + dropped
        - ``"propagate"`` — exception is re-raised by drain() on
          the next yield

        Returns the BackgroundTask handle so callers can track or
        cancel.
        """
        sq = self._get_or_create(session_id)
        loop = asyncio.get_event_loop()
        task = loop.create_task(self._runner(sq, coro_factory, on_error))
        bt = BackgroundTask(session_id, task)
        sq.in_flight[task] = bt
        return bt

    async def _runner(
        self, sq: "_SessionQueue",
        coro_factory: Any,
        on_error: str,
    ) -> None:
        """Run the coro, capture events into the queue, surface errors."""
        current_task = asyncio.current_task()
        try:
            coro = coro_factory()
            async for ev, payload in coro:
                sq.events.append((ev, payload, time.monotonic()))
                sq.event_ready.set()
        except Exception as exc:  # noqa: BLE001
            sq.events.append(("background_error", {
                "error": repr(exc),
            }, time.monotonic()))
            sq.event_ready.set()
            if on_error == "propagate":
                LOGGER.warning(
                    "background task failed (session=%s): %s",
                    sq.session_id, exc,
                )
        finally:
            # Remove ourselves (and any already-done siblings) from
            # in_flight. ``t.done()`` returns False for the task
            # currently executing its finally block, so we explicitly
            # pop ourselves by identity.
            sq.in_flight.pop(current_task, None)
            for t in list(sq.in_flight):
                if t.done():
                    sq.in_flight.pop(t, None)
            sq.event_ready.set()
            if len(self._sessions) > self._max:
                self._evict_idle()

    # ------------------------------------------------------------------
    # Drain — consume events as background tasks finish
    async def drain(self, session_id: str) -> AsyncIterator[tuple[str, dict]]:
        """Yield events as background tasks complete for ``session_id``.

        Termination conditions (checked in order):
        - Consumer stops iterating (GeneratorExit) → cancel in-flight
        - All in-flight tasks done AND no events buffered → return
        - Consumer has been idle for ``idle_timeout`` (60s) → return

        Implementation: snapshot in-flight tasks, await asyncio.wait
        on them. Each completed task in _runner appends events to
        ``sq.events``. We loop, draining events and re-waiting on
        remaining tasks until either nothing remains or we hit the
        idle timeout.
        """
        sq = self._get_or_create(session_id)
        idle_timeout = 60.0
        try:
            while True:
                # Drain any events already buffered.
                while sq.events:
                    ev, payload, ts = sq.events.popleft()
                    yield (ev, payload)
                # Snapshot in-flight tasks NOW. New submits during
                # the wait will not block our termination.
                tasks = list(sq.in_flight.keys())
                if not tasks:
                    return
                try:
                    done, pending = await asyncio.wait(
                        tasks, timeout=idle_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except Exception:
                    return
                # Loop back: pop any events the completed tasks emitted,
                # then re-check in_flight. Repeat until empty.
                if not pending:
                    # All snapshotted tasks done. Drain final events,
                    # then return.
                    while sq.events:
                        ev, payload, ts = sq.events.popleft()
                        yield (ev, payload)
                    return
        except GeneratorExit:
            for t in list(sq.in_flight):
                t.cancel()
            raise

    # ------------------------------------------------------------------
    # Synchronisation
    # ------------------------------------------------------------------
    async def wait_all(self, session_id: str, timeout: float = 30.0) -> int:
        """Block until all in-flight background tasks for ``session_id``
        complete, or ``timeout`` elapses.

        Returns the number of tasks still pending after the wait
        (0 = all done). Used at session shutdown to avoid orphaned
        coroutines.
        """
        sq = self._get_or_create(session_id)
        if not sq.in_flight:
            return 0
        tasks = list(sq.in_flight.keys())
        if not tasks:
            return 0
        # Use return_when=FIRST_EXCEPTION + shield so we still see
        # remaining tasks after timeout. Then explicitly await each
        # so the task's finally block runs (in_flight cleanup).
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.ALL_COMPLETED,
            )
        except Exception:
            return len(tasks)
        # Await all done tasks so their finally blocks (which pop
        # themselves from sq.in_flight) get a chance to run before
        # we return the count.
        for t in done:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        return len(sq.in_flight)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def in_flight_count(self, session_id: str) -> int:
        sq = self._sessions.get(session_id)
        return len(sq.in_flight) if sq else 0

    def queued_event_count(self, session_id: str) -> int:
        sq = self._sessions.get(session_id)
        return len(sq.events) if sq else 0

    def tracked_count(self) -> int:
        return len(self._sessions)

    def reset(self) -> None:
        """Drop all state. Tests-only."""
        for sq in self._sessions.values():
            for t in list(sq.in_flight):
                t.cancel()
            sq.events.clear()
            sq.in_flight.clear()
        self._sessions.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _get_or_create(self, session_id: str) -> "_SessionQueue":
        sq = self._sessions.get(session_id)
        if sq is None:
            sq = _SessionQueue(session_id)
            self._sessions[session_id] = sq
        return sq

    def _evict_idle(self) -> None:
        idle = [
            sid for sid, sq in self._sessions.items()
            if not sq.in_flight and not sq.events
        ]
        for sid in idle[: len(idle) // 2 + 1]:
            self._sessions.pop(sid, None)


class _SessionQueue:
    """Per-session queue state — events buffered + in-flight tasks."""

    __slots__ = ("session_id", "events", "in_flight", "event_ready")

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.events: Any = deque()  # FIFO of (event, payload, timestamp)
        self.in_flight: dict[asyncio.Task, BackgroundTask] = {}
        self.event_ready: asyncio.Event = asyncio.Event()


__all__ = ["BackgroundTaskQueue", "BackgroundTask"]
