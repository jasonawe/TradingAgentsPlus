"""Step 39 — async Read-Write lock primitive.

Pure lock object — no per-session bookkeeping. Pair with
``SessionRWLockManager`` to get one RWLock per ``session_id``.

Semantics
---------
- ``acquire_read()``: multiple concurrent readers allowed.
- ``acquire_write()``: exclusive; blocks until all current readers
  release and any pending writers drain.
- Writer priority — when a writer is waiting, new readers block to
  prevent writer starvation.
- Sync ``acquire_read_sync`` / ``acquire_write_sync`` mirror for tests
  + non-async callers (uses threading.Event instead of asyncio.Condition).

Why a fresh primitive instead of ``asyncio.Lock``
------------------------------------------------
``asyncio.Lock`` is a mutex. The chat session pattern is "many readers,
one writer" — multi-tab frontends subscribe to the same SSE stream,
the audit viewer polls, the LLM judge queries plan / verdict while
the orchestrator mutates them. A mutex serialises all of those against
the writer, which defeats the point.

This lock is fair (FIFO-ish via Condition.wait) and writer-priority
so the orchestrator never starves behind a flood of read pollers.

Drop-in
-------
``acquire_read`` / ``acquire_write`` return context managers. Used
directly::

    lock = SessionRWLock()
    async with lock.acquire_read():
        snapshot = read_state()
    async with lock.acquire_write():
        mutate_state()
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import AsyncIterator, Iterator


class SessionRWLock:
    """Async RWLock with writer-priority fairness."""

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    # ------------------------------------------------------------------
    # Async API
    # ------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def acquire_read(self) -> AsyncIterator[None]:
        """Acquire a read lock. Multiple readers allowed concurrently."""
        async with self._cond:
            # Writer priority: if a writer is waiting or active, block.
            while self._writer_active or self._writers_waiting > 0:
                await self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextlib.asynccontextmanager
    async def acquire_write(self) -> AsyncIterator[None]:
        """Acquire a write lock. Blocks until readers + other writers drain."""
        async with self._cond:
            self._writers_waiting += 1
            while self._writer_active or self._readers > 0:
                await self._cond.wait()
            self._writers_waiting -= 1
            self._writer_active = True
        try:
            yield
        finally:
            async with self._cond:
                self._writer_active = False
                self._cond.notify_all()

    # ------------------------------------------------------------------
    # Introspection (debug + tests)
    # ------------------------------------------------------------------
    @property
    def reader_count(self) -> int:
        return self._readers

    @property
    def writer_active(self) -> bool:
        return self._writer_active

    @property
    def writers_waiting(self) -> int:
        return self._writers_waiting


class SessionRWLockSync:
    """Synchronous RWLock (threading) — for non-async callers and tests.

    Same semantics as SessionRWLock but uses threading.Event / Condition.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    @contextlib.contextmanager
    def acquire_read(self) -> Iterator[None]:
        with self._cond:
            while self._writer_active or self._writers_waiting > 0:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextlib.contextmanager
    def acquire_write(self) -> Iterator[None]:
        with self._cond:
            self._writers_waiting += 1
            while self._writer_active or self._readers > 0:
                self._cond.wait()
            self._writers_waiting -= 1
            self._writer_active = True
        try:
            yield
        finally:
            with self._cond:
                self._writer_active = False
                self._cond.notify_all()


__all__ = ["SessionRWLock", "SessionRWLockSync"]
