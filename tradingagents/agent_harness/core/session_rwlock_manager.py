"""Step 39 — per-session RWLock manager.

Holds one ``SessionRWLock`` per ``session_id``. Provides the same
``run()`` shape as ``SessionLockManager`` so it can be used as a
drop-in replacement, plus ``acquire_read`` / ``acquire_write`` for
non-stream callers (audit viewer, LLM judge, status endpoint).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any, AsyncIterator, Callable

from .session_rwlock import SessionRWLock

LOGGER = logging.getLogger(__name__)

_MAX_TRACKED_SESSIONS = 1024


class SessionRWLockManager:
    """One async RWLock per session_id with the same surface as
    ``SessionLockManager.run``.
    """

    def __init__(self, max_sessions: int = _MAX_TRACKED_SESSIONS) -> None:
        self._locks: dict[str, SessionRWLock] = {}
        self._max = max_sessions

    # ------------------------------------------------------------------
    # Public — wrap an async generator with the per-session WRITE lock
    # ------------------------------------------------------------------
    async def run(
        self,
        session_id: str,
        producer: Callable[[], AsyncIterator[tuple[str, dict[str, Any]]]],
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Run ``producer()`` under the write lock for ``session_id``.

        If another request is already running for this ``session_id``,
        yields a single ``busy`` event and exits without invoking
        ``producer``. Same shape as ``SessionLockManager.run`` so the
        two are drop-in interchangeable.
        """
        lock = self._get_or_create(session_id)
        if lock.writer_active or lock.writers_waiting > 0:
            yield (
                "busy",
                {
                    "session_id": session_id,
                    "message": "previous request for this session is in-flight",
                },
            )
            return

        async with lock.acquire_write():
            run_id = uuid.uuid4().hex[:12]
            LOGGER.debug(
                "session rwlock (write) acquired: %s (run_id=%s)",
                session_id, run_id,
            )
            try:
                async for event, payload in producer():
                    yield event, payload
            finally:
                LOGGER.debug(
                    "session rwlock (write) released: %s", session_id
                )
                if len(self._locks) > self._max:
                    self._evict_idle()

    # ------------------------------------------------------------------
    # Public — read / write lock acquisition for non-stream callers
    # ------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def acquire_read(self, session_id: str):
        """Acquire a read lock on the session state."""
        lock = self._get_or_create(session_id)
        async with lock.acquire_read():
            yield

    @contextlib.asynccontextmanager
    async def acquire_write(self, session_id: str):
        """Acquire a write lock on the session state."""
        lock = self._get_or_create(session_id)
        async with lock.acquire_write():
            yield

    # ------------------------------------------------------------------
    # Introspection (tests + observability)
    # ------------------------------------------------------------------
    def is_busy(self, session_id: str) -> bool:
        """True if a write request is currently in-flight."""
        lock = self._locks.get(session_id)
        return bool(lock and (lock.writer_active or lock.writers_waiting > 0))

    def reader_count(self, session_id: str) -> int:
        lock = self._locks.get(session_id)
        return lock.reader_count if lock else 0

    def tracked_count(self) -> int:
        return len(self._locks)

    def reset(self) -> None:
        """Drop all locks. Tests-only."""
        self._locks.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _get_or_create(self, session_id: str) -> SessionRWLock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = SessionRWLock()
            self._locks[session_id] = lock
        return lock

    def _evict_idle(self) -> None:
        idle = [
            k for k, lk in self._locks.items()
            if lk.reader_count == 0 and not lk.writer_active
            and lk.writers_waiting == 0
        ]
        for k in idle[: len(idle) // 2 + 1]:
            self._locks.pop(k, None)


__all__ = ["SessionRWLockManager"]
