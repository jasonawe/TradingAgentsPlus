"""Tracer — lightweight in-memory trace log (v3 spec §7.2 #8 OpenTelemetry-lite)."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class TraceSpan:
    name: str
    started_at: float
    ended_at: float | None = None
    trace_id: str = ""
    parent_id: str | None = None
    attrs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "attrs": dict(self.attrs),
        }


class Tracer:
    """Append-only in-memory tracer. Persist on shutdown if needed."""

    def __init__(self) -> None:
        self.spans: list[TraceSpan] = []

    def start(self, name: str, *, trace_id: str | None = None, parent_id: str | None = None) -> TraceSpan:
        span = TraceSpan(
            name=name,
            started_at=time.time(),
            trace_id=trace_id or uuid.uuid4().hex[:12],
            parent_id=parent_id,
        )
        self.spans.append(span)
        return span

    def end(self, span: TraceSpan, **attrs) -> None:
        span.ended_at = time.time()
        span.attrs.update(attrs)

    def snapshot(self) -> list[dict]:
        return [s.to_dict() for s in self.spans[-200:]]  # last 200 spans
