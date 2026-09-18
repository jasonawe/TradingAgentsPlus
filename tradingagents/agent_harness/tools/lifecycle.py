"""Step 23 — lifecycle tracker for tool pre / post / error hooks.

Captures per-tool-call events with timing data so the L3 judge and
audit log can correlate what happened to a tool invocation. We do
NOT add async hooks here — every builtin runs through the same
``invoke()`` wrapper which already calls the tracker.

This is deliberately small + synchronous; async hooks add a lot of
plumbing for negligible UX value.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LifecycleEvent:
    """One tool call event. ``phase`` is one of pre / post / error."""
    phase: str
    tool: str
    at: float = field(default_factory=time.time)
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    detail: str = ""


class LifecycleTracker:
    """In-memory ring buffer of tool-call events.

    Not thread-safe — the harness calls invoke() under a lock so the
    order is preserved. For production cross-process tracking, this
    is replaced by the audit log writer in ``core.audit``.
    """

    def __init__(self, capacity: int = 256) -> None:
        self._events: list[LifecycleEvent] = []
        self._capacity = capacity

    @property
    def events(self) -> list[LifecycleEvent]:
        return list(self._events)

    def pre(self, tool: str, *, args: dict[str, Any] | None = None) -> None:
        self._append(LifecycleEvent(
            phase="pre",
            tool=tool,
            args=args or {},
        ))

    def post(self, tool: str, *, args: dict[str, Any] | None = None,
             result: Any = None) -> None:
        self._append(LifecycleEvent(
            phase="post",
            tool=tool,
            args=args or {},
            result=result,
        ))

    def error(self, tool: str, *, args: dict[str, Any] | None = None,
              error: BaseException | None = None) -> None:
        detail = ""
        if error is not None:
            detail = f"{type(error).__name__}: {error}"
        self._append(LifecycleEvent(
            phase="error",
            tool=tool,
            args=args or {},
            error=detail or "unknown",
        ))

    def clear(self) -> None:
        self._events.clear()

    def _append(self, ev: LifecycleEvent) -> None:
        self._events.append(ev)
        if len(self._events) > self._capacity:
            # drop oldest
            self._events = self._events[-self._capacity:]


_global_tracker = LifecycleTracker()


def global_tracker() -> LifecycleTracker:
    """Default process-wide tracker for harness-level instrumentation."""
    return _global_tracker
