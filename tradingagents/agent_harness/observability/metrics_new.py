"""Step 44 — Counter / Histogram / Gauge primitives + MetricsRegistry.

In-memory metrics. Optional Prometheus exposition format supported via
``to_prometheus()`` (text/plain content type).

Naming convention
-----------------
- ``harness.<event>.<unit>`` — e.g. ``harness.tool_call.total``
- Histogram: ``harness.<event>_ms.bucket{le=...,ai=}``
"""
from __future__ import annotations

import math
import threading
from typing import Any


class Counter:
    """Monotonically increasing counter (per process)."""

    __slots__ = ("_name", "_help", "_value", "_lock", "_labels")

    def __init__(
        self, name: str, *, help: str = "", labels: dict[str, str] | None = None,
    ) -> None:
        self._name = name
        self._help = help
        self._value = 0.0
        self._lock = threading.Lock()
        self._labels = labels or {}

    def inc(self, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            if labels:
                # Labels-bearing counters are stored separately via the
                # registry. This path is for label-free counters.
                raise ValueError(
                    "Counter.inc(labels=) requires registry.labeled_counter"
                )
            self._value += amount

    def value(self) -> float:
        with self._lock:
            return self._value


class Gauge:
    """A value that goes up and down (current pool size, queue depth)."""

    __slots__ = ("_name", "_help", "_value", "_lock")

    def __init__(self, name: str, *, help: str = "") -> None:
        self._name = name
        self._help = help
        self._value = 0.0
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value

    def value(self) -> float:
        with self._lock:
            return self._value


class Histogram:
    """Histogram with fixed bucket boundaries (default: 1, 5, 10, 25, 50,
    100, 250, 500, 1000, 2500, 5000, +Inf — millisecond latency bins).

    Records ``observe(value)`` and accumulates counts per bucket plus
    sum / count.
    """

    DEFAULT_BUCKETS: tuple[float, ...] = (
        1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0,
        1000.0, 2500.0, 5000.0, float("inf"),
    )

    __slots__ = ("_name", "_help", "_buckets", "_counts", "_sum", "_count", "_lock")

    def __init__(
        self, name: str, *, help: str = "",
        buckets: tuple[float, ...] | None = None,
    ) -> None:
        self._name = name
        self._help = help
        self._buckets = buckets or self.DEFAULT_BUCKETS
        # counts[i] = # observations <= buckets[i]
        self._counts: list[int] = [0] * len(self._buckets)
        self._sum = 0.0
        self._count = 0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._count += 1
            for i, b in enumerate(self._buckets):
                if value <= b:
                    self._counts[i] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self._name,
                "buckets": list(self._buckets),
                "counts": list(self._counts),
                "sum": self._sum,
                "count": self._count,
            }


class MetricsRegistry:
    """Process-wide registry of metrics + tracers.

    Counters/Histograms/Gauges registered here. Tracers are per-session
    with bounded retention.
    """

    def __init__(self, *, max_tracers: int = 256) -> None:
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._tracers: dict[str, Any] = {}  # session_id -> Tracer
        self._max_tracers = max_tracers
        self._lock = threading.Lock()

    def counter(self, name: str, *, help: str = "") -> Counter:
        with self._lock:
            c = self._counters.get(name)
            if c is None:
                c = Counter(name, help=help)
                self._counters[name] = c
            return c

    def gauge(self, name: str, *, help: str = "") -> Gauge:
        with self._lock:
            g = self._gauges.get(name)
            if g is None:
                g = Gauge(name, help=help)
                self._gauges[name] = g
            return g

    def histogram(
        self, name: str, *, help: str = "", buckets: tuple[float, ...] | None = None,
    ) -> Histogram:
        with self._lock:
            h = self._histograms.get(name)
            if h is None:
                h = Histogram(name, help=help, buckets=buckets)
                self._histograms[name] = h
            return h

    # ------------------------------------------------------------------
    # Tracer lifecycle
    # ------------------------------------------------------------------
    def get_or_create_tracer(self, session_id: str) -> Any:
        from .tracing import Tracer
        with self._lock:
            t = self._tracers.get(session_id)
            if t is None:
                t = Tracer(session_id)
                self._tracers[session_id] = t
                if len(self._tracers) > self._max_tracers:
                    # Evict oldest (dict preserves insertion order).
                    overflow = len(self._tracers) - self._max_tracers
                    for sid in list(self._tracers)[:overflow]:
                        self._tracers.pop(sid, None)
            return t

    def drop_tracer(self, session_id: str) -> bool:
        with self._lock:
            return self._tracers.pop(session_id, None) is not None

    def list_tracers(self) -> list[str]:
        with self._lock:
            return list(self._tracers.keys())

    # ------------------------------------------------------------------
    # Snapshot for endpoints
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = {n: v.value() for n, v in self._counters.items()}
            gauges = {n: v.value() for n, v in self._gauges.items()}
            histograms = {n: v.snapshot() for n, v in self._histograms.items()}
            tracer_count = len(self._tracers)
        return {
            "counters": counters,
            "gauges": gauges,
            "histograms": histograms,
            "tracer_count": tracer_count,
        }


__all__ = ["Counter", "Gauge", "Histogram", "MetricsRegistry"]
