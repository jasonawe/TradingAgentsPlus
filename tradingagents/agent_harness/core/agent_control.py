"""Q4 / P1-4 (roadmap §11.4 + §12.6) — Agent runtime control primitives.

Three orthogonal capabilities plug into ``Orchestrator``:

- ``steer(session_id, message)`` — append a *user-voice* directive that
  takes effect on the **next step** of the current turn (e.g. "switch to
  fundamentals, ignore the price"). The bus consumes it before the next
  ``tool.invoke`` so the LLM sees it immediately.

- ``inject(session_id, message, when='next_idle')`` — append a
  *system-voice* directive that surfaces at a defined moment. The
  default ``next_idle`` adds the message to the *next user turn's*
  context as a hidden system hint; ``immediate`` injects straight into
  the current run if the orchestrator is still spinning, else falls
  back to ``next_idle``.

- ``when_idle(session_id, callback)`` / ``when_any_idle(callback)`` —
  register a callback that fires once the orchestrator finishes a turn
  (success, error, or interruption). Used by scheduled tasks ("every
  market close, run analysis on AAPL") and observability hooks
  ("publish SSE done event").

Design notes:
  - The bus is **process-wide**, keyed by ``session_id``.  Multiple
    Orchestrator instances (tests, parallel workers) share the same
    bus instance when wired to the same Harness.
  - Steers / injects are *one-shot* — once consumed they disappear.
    Persistent preferences go through L2 memory, not the control bus.
  - ``when_idle`` callbacks are best-effort and isolated: a callback
    that raises is logged and skipped; other callbacks still fire.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

LOGGER = logging.getLogger(__name__)

InjectWhen = Literal["next_idle", "immediate"]


@dataclass(frozen=True)
class SteerMessage:
    message: str
    author: str
    ts: float


@dataclass(frozen=True)
class InjectMessage:
    message: str
    when: InjectWhen
    author: str
    ts: float


@dataclass
class _IdleCallback:
    callback: Callable[[str, Any], Awaitable[None] | None]
    scope: Literal["session", "global"]
    session_id: str | None = None


class AgentControlBus:
    """Process-wide bus for steer / inject / whenIdle runtime control."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._steers: dict[str, list[SteerMessage]] = {}
        self._injects: dict[str, list[InjectMessage]] = {}
        self._idle_callbacks: list[_IdleCallback] = []

    # ------------------------------------------------------------------
    # Producers
    # ------------------------------------------------------------------
    def steer(
        self,
        session_id: str,
        message: str,
        *,
        author: str = "external",
    ) -> int:
        """Queue a user-voice directive for ``session_id``.

        Returns the queue depth after the push (1 = first steer).
        """
        if not message:
            raise ValueError("steer message must be non-empty")
        with self._lock:
            self._steers.setdefault(session_id, []).append(
                SteerMessage(message=message, author=author, ts=time.time())
            )
            return len(self._steers[session_id])

    def inject(
        self,
        session_id: str,
        message: str,
        *,
        when: InjectWhen = "next_idle",
        author: str = "external",
    ) -> int:
        """Queue a system-voice directive for ``session_id``."""
        if not message:
            raise ValueError("inject message must be non-empty")
        if when not in ("next_idle", "immediate"):
            raise ValueError(f"unknown inject when={when!r}")
        with self._lock:
            self._injects.setdefault(session_id, []).append(
                InjectMessage(message=message, when=when, author=author, ts=time.time())
            )
            return len(self._injects[session_id])

    def when_idle(
        self,
        callback: Callable[[str, Any], Awaitable[None] | None],
        session_id: str | None = None,
    ) -> None:
        """Register a callback fired when ``session_id`` (or any session)
        becomes idle. ``session_id=None`` registers a global callback.
        """
        scope: Literal["session", "global"] = "session" if session_id else "global"
        cb = _IdleCallback(callback=callback, scope=scope, session_id=session_id)
        with self._lock:
            self._idle_callbacks.append(cb)

    def cancel_when_idle(
        self,
        callback: Callable[..., Any],
        session_id: str | None = None,
    ) -> int:
        """Unregister idle callbacks matching ``callback`` (and ``session_id``).

        Returns the count removed. Useful for cleanup in tests and for
        scheduled tasks that should only run once.
        """
        with self._lock:
            kept: list[_IdleCallback] = []
            removed = 0
            for cb in self._idle_callbacks:
                same = cb.callback is callback and (
                    session_id is None or cb.session_id == session_id
                )
                if same:
                    removed += 1
                else:
                    kept.append(cb)
            self._idle_callbacks = kept
            return removed

    # ------------------------------------------------------------------
    # Consumers (called by Orchestrator)
    # ------------------------------------------------------------------
    def consume_steers(self, session_id: str) -> list[SteerMessage]:
        """Atomically pop all queued steers for ``session_id``."""
        with self._lock:
            return list(self._steers.pop(session_id, []))

    def consume_injects(
        self,
        session_id: str,
        *,
        when: InjectWhen | None = None,
    ) -> list[InjectMessage]:
        """Atomically pop injects for ``session_id`` matching ``when``.

        ``when=None`` returns *all* injects regardless of ``when``.
        Non-matching injects are returned to the queue so a subsequent
        ``consume_injects(when='immediate')`` call can still see them.
        """
        with self._lock:
            pending = list(self._injects.get(session_id, []))
            if when is None:
                self._injects[session_id] = []
                return pending
            matched = [m for m in pending if m.when == when]
            self._injects[session_id] = [m for m in pending if m.when != when]
            if not self._injects[session_id]:
                self._injects.pop(session_id, None)
            return matched

    def peek_pending(self, session_id: str) -> dict[str, int]:
        """Non-mutating count of queued steers / injects (for diagnostics)."""
        with self._lock:
            return {
                "steers": len(self._steers.get(session_id, [])),
                "injects": len(self._injects.get(session_id, [])),
            }

    def idle_callback_count(
        self,
        session_id: str | None = None,
    ) -> int:
        with self._lock:
            if session_id is None:
                return len(self._idle_callbacks)
            return sum(
                1
                for cb in self._idle_callbacks
                if cb.scope == "global" or cb.session_id == session_id
            )

    # ------------------------------------------------------------------
    # Idle notification
    # ------------------------------------------------------------------
    async def notify_idle(self, session_id: str, *, state: Any = None) -> int:
        """Fire all matching idle callbacks. Returns the number fired.

        Callbacks may be sync or async; failures are isolated and logged.
        """
        with self._lock:
            targets = [
                cb for cb in self._idle_callbacks
                if cb.scope == "global" or cb.session_id == session_id
            ]
        fired = 0
        for cb in targets:
            try:
                result = cb.callback(session_id, state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                LOGGER.warning(
                    "whenIdle callback failed for session=%s (scope=%s)",
                    session_id, cb.scope, exc_info=True,
                )
            fired += 1
        return fired


__all__ = ["AgentControlBus", "SteerMessage", "InjectMessage"]
