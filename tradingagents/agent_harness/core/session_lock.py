"""Per-session in-flight lock — prevents concurrent stream_chat for same session.

Why this exists
---------------
``Orchestrator.stream_chat(session_id=..., ...)`` is an async generator
that mutates ``OrchestratorState`` (plan, tool_results, audit log,
L3 verdict) and emits SSE events. If two requests for the same
``session_id`` arrive concurrently (browser double-click, mobile app
auto-reconnect, parallel ``ab`` tests), they:

- race on circuit-breaker success/failure counts
- emit interleaved events into the same SSE stream
- double-charge token accounting
- corrupt the L3 judge's plan-replan list

The lock guarantees only one ``stream_chat`` per ``session_id`` runs at
a time. Subsequent requests see a ``busy`` event and exit immediately
(frontend should render "previous request still in progress, please wait").

Usage
-----
::

    manager = SessionLockManager()
    orch = Orchestrator(...)

    async for event, payload in manager.run(
        session_id,
        lambda: orch.stream_chat(session_id=session_id, user_message=msg),
    ):
        yield event, payload

If another request is already running for ``session_id``, the second
``manager.run`` call yields a single ``busy`` event with the active
task's id and exits — no orchestrator work runs.

Memory
------
Locks are stored in a ``WeakValueDictionary`` when possible so stale
sessions garbage-collect. For asyncio.Lock we use a plain dict and
evict entries when the count exceeds a soft cap.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncIterator, Callable

LOGGER = logging.getLogger(__name__)


# Soft cap on tracked sessions — beyond this we evict the oldest idle
# locks (ones whose tasks have finished).  Keeps memory bounded under
# pathological session-id churn.
_MAX_TRACKED_SESSIONS = 1024


class SessionLockManager:
    """One asyncio.Lock per session_id, shared across all callers."""

    def __init__(self, max_sessions: int = _MAX_TRACKED_SESSIONS) -> None:
        self._locks: dict[str, _SessionEntry] = {}
        self._max = max_sessions

    # ------------------------------------------------------------------
    # Public — wrap an async generator with the per-session lock
    # ------------------------------------------------------------------
    async def run(
        self,
        session_id: str,
        producer: Callable[[], AsyncIterator[tuple[str, dict[str, Any]]]],
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Run ``producer()`` under the lock for ``session_id``.

        Yields every event from ``producer``. If another request is
        already running for this ``session_id``, yields a single
        ``busy`` event and exits without invoking ``producer``.

        Parameters
        ----------
        session_id:
            Logical session key (e.g. ``harness-{uuid}`` or a user-chosen
            conversation id). Locked per key.
        producer:
            Callable returning an async iterator of ``(event, payload)``
            tuples. Wrapped by an ``await acquire()`` before any work.
        """
        entry = self._get_or_create(session_id)
        # Non-blocking try-lock — fail fast on contention.
        if entry.locked():
            yield (
                "busy",
                {
                    "session_id": session_id,
                    "active_run_id": entry.run_id,
                    "message": "previous request for this session is in-flight",
                },
            )
            return

        async with entry.lock:
            entry.run_id = uuid.uuid4().hex[:12]
            LOGGER.debug("session lock acquired: %s (run_id=%s)", session_id, entry.run_id)
            try:
                async for event, payload in producer():
                    yield event, payload
            finally:
                # Cleanup if we're way over the cap.
                if len(self._locks) > self._max:
                    self._evict_idle()
                LOGGER.debug("session lock released: %s", session_id)

    # ------------------------------------------------------------------
    # Introspection (tests + observability)
    # ------------------------------------------------------------------
    def is_busy(self, session_id: str) -> bool:
        """True if a request is currently in-flight for ``session_id``."""
        entry = self._locks.get(session_id)
        return bool(entry and entry.locked())

    def active_run_id(self, session_id: str) -> str | None:
        """Return the run_id of the in-flight request, or None."""
        entry = self._locks.get(session_id)
        return entry.run_id if entry and entry.locked() else None

    def tracked_count(self) -> int:
        """How many session locks are currently tracked (occupied or not)."""
        return len(self._locks)

    def reset(self) -> None:
        """Drop all locks. Tests-only — production code should not call this."""
        self._locks.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _get_or_create(self, session_id: str) -> "_SessionEntry":
        entry = self._locks.get(session_id)
        if entry is None:
            entry = _SessionEntry(lock=asyncio.Lock(), run_id=None)
            self._locks[session_id] = entry
        return entry

    def _evict_idle(self) -> None:
        """Remove locks whose asyncio.Lock is not currently held.

        Cheap — we only inspect the ``_value_get`` flag, not actually
        acquire anything. Safe to call while other sessions are running.
        """
        idle = [k for k, e in self._locks.items() if not e.lock.locked()]
        # Remove the oldest half so we don't thrash.
        for k in idle[: len(idle) // 2 + 1]:
            self._locks.pop(k, None)


class _SessionEntry:
    """One session's lock + the run_id of whoever currently holds it."""

    __slots__ = ("lock", "run_id")

    def __init__(self, lock: asyncio.Lock, run_id: str | None) -> None:
        self.lock = lock
        self.run_id = run_id

    def locked(self) -> bool:
        return self.lock.locked()
