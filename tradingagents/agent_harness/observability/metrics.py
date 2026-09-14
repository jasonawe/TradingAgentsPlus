"""Metrics — token / latency / error counters (v3 spec §7.2 #4)."""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Bucket:
    count: int = 0
    total: float = 0.0
    errors: int = 0

    def observe(self, value: float, error: bool = False) -> None:
        self.count += 1
        self.total += value
        if error:
            self.errors += 1

    def snapshot(self) -> dict[str, Any]:
        avg = (self.total / self.count) if self.count else 0.0
        return {
            "count": self.count,
            "errors": self.errors,
            "avg_latency_ms": round(avg * 1000, 2),
        }


@dataclass
class Metrics:
    """Lightweight in-memory counters. Persist to disk on demand."""

    started_at: float = field(default_factory=time.time)
    _buckets: dict[str, _Bucket] = field(default_factory=lambda: defaultdict(_Bucket))

    def observe(self, name: str, latency_seconds: float, *, error: bool = False) -> None:
        self._buckets[name].observe(latency_seconds, error=error)

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime_seconds": round(time.time() - self.started_at, 2),
            "counters": {k: v.snapshot() for k, v in sorted(self._buckets.items())},
        }

    def reset(self) -> None:
        self._buckets.clear()
        self.started_at = time.time()
