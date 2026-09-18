"""Step 44 — Tracer + Span primitives.

Lightweight tracing model (no OpenTelemetry dependency):

- ``Span`` records a single operation's start, end, attributes
- ``Tracer`` is a session-scoped factory: one tracer per session_id,
  spans within share a ``trace_id`` (uuid).
- ``current_trace_id()`` returns the trace id of the most recent span
  on the current asyncio context (used by LLM calls to tag requests).

Memory:
- Last N spans kept per session in a bounded ring buffer (default 64).
- Eviction: when a session exceeds cap, oldest spans drop.
"""
from __future__ import annotations

import contextvars
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Span:
    """One traced operation.

    Attributes
    ----------
    span_id : str
    trace_id : str
        All spans in the same tracer share this id
    parent_id : str | None
        Id of the parent span (None for root spans)
    name : str
        Operation name (e.g. "plan", "execute", "verify")
    started_at : float
        time.monotonic() at start
    ended_at : float | None
        time.monotonic() at end (None while in-flight)
    attributes : dict[str, Any]
        Free-form metadata (intent, op, tool_name, etc.)
    status : str
        "ok" / "error" / "running"
    error : str | None
        Exception repr when status == "error"
    """

    span_id: str
    trace_id: str
    parent_id: str | None
    name: str
    started_at: float
    ended_at: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    error: str | None = None

    def duration_ms(self) -> float:
        if self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at) * 1000.0


class Tracer:
    """Session-scoped trace container."""

    def __init__(self, session_id: str = "", *, max_spans: int = 64) -> None:
        self.session_id = session_id
        self.trace_id = uuid.uuid4().hex[:12]
        self._max = max_spans
        self._spans: list[Span] = []
        self._open: dict[str, Span] = {}

    def start(
        self,
        name: str,
        *,
        parent_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        """Open a new span and return it (caller ends it)."""
        span = Span(
            span_id=uuid.uuid4().hex[:8],
            trace_id=self.trace_id,
            parent_id=parent_id,
            name=name,
            started_at=time.monotonic(),
            attributes=dict(attributes or {}),
        )
        self._open[span.span_id] = span
        return span

    def end(
        self,
        span: Span,
        *,
        status: str = "ok",
        error: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Close a span. Final attributes may be appended."""
        span.ended_at = time.monotonic()
        span.status = status
        span.error = error
        if attributes:
            span.attributes.update(attributes)
        self._spans.append(span)
        self._open.pop(span.span_id, None)
        if len(self._spans) > self._max:
            # Drop oldest, keep recent.
            overflow = len(self._spans) - self._max
            del self._spans[:overflow]

    def spans(self) -> list[Span]:
        """Snapshot of completed spans (read-only)."""
        return list(self._spans)

    def open_spans(self) -> list[Span]:
        """Snapshot of still-running spans."""
        return list(self._open.values())

    def to_dict(self) -> dict[str, Any]:
        """Render the trace as a JSON-friendly dict (for endpoints)."""
        return {
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "span_count": len(self._spans),
            "spans": [
                {
                    "span_id": s.span_id,
                    "parent_id": s.parent_id,
                    "name": s.name,
                    "started_at": s.started_at,
                    "ended_at": s.ended_at,
                    "duration_ms": s.duration_ms(),
                    "status": s.status,
                    "error": s.error,
                    "attributes": s.attributes,
                }
                for s in self._spans
            ],
        }


# asyncio contextvar — current_trace_id for cross-function propagation
_current_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_trace_id", default=None,
)


def current_trace_id() -> str | None:
    """Return the trace id of the most recent caller's tracer, or None."""
    return _current_trace_id.get()


def set_current_trace_id(trace_id: str) -> contextvars.Token:
    return _current_trace_id.set(trace_id)


__all__ = ["Tracer", "Span", "current_trace_id", "set_current_trace_id"]
