"""Q1 / P2-4 — Turn/Step boundary + TurnEndReason (roadmap §12.6 P0-M3).

A ``Turn`` is one user-message-to-final-response round in ``stream_chat``.
A ``Step`` is a single orchestrator node visit (plan / execute / observe /
verify / synthesize). Each turn and step gets a monotonic ID so SSE
clients and the event log can correlate events, and each turn records
the reason it ended so observability tooling can spot abnormal exits.

Why this exists:
  - Today the orchestrator emits ``plan_started`` / ``plan_ready`` /
    ``tool_result`` / etc. but has no explicit turn boundary marker, so
    distinguishing "one user turn" from "ten retried LLM calls" is
    impossible without timestamp math.
  - Without ``TurnEndReason``, dashboards can only show a green
    completion flag — they can't filter on "all turns that exited
    because of an inject" or "all turns that ended mid-execute due to
    a steer".

The boundary also feeds back into the event log: every ``turn/started``
and ``turn/ended`` is committed as a ``surface=LOG`` event for replay,
matching the dsh session.md "end-seed" semantic (the end marker is
what makes the turn replayable).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class TurnEndReason(str, Enum):
    """Why a turn stopped. Stored in ``turn/ended`` payload + audit log."""

    # Happy path
    USER_DONE = "user_done"               # all nodes completed; final emitted
    # External control (Q4 / P1-4)
    STEERED = "steered"                   # an active steer changed flow mid-turn
    INJECTED = "injected"                 # an inject surfaced as a system hint
    # Failure
    ERROR = "error"                       # exception in any node
    TIMEOUT = "timeout"                   # exceeded turn-level deadline
    INTERRUPTED = "interrupted"           # client disconnected / cancelled
    # Edge cases
    EMPTY = "empty"                       # no plan → nothing to execute
    SHORT_CIRCUIT = "short_circuit"       # Tier 1 returned without 5-node flow


# Default step "name" when emitting step/started; mirrors harness_checkpoint.
StepName = Literal[
    "planning", "executing", "observing",
    "verifying", "synthesizing", "done",
]


@dataclass
class StepRecord:
    """A single node visit inside a turn."""

    step_id: int
    name: StepName
    started_at: float
    finished_at: float | None = None
    status: Literal["ok", "error", "skipped"] | None = None
    error: str | None = None

    @property
    def duration_s(self) -> float | None:
        if self.finished_at is None:
            return None
        return self.finished_at - self.started_at


@dataclass
class TurnRecord:
    """The full lifecycle of one ``stream_chat`` call."""

    turn_id: int
    session_id: str
    user_message: str
    started_at: float
    finished_at: float | None = None
    end_reason: TurnEndReason | None = None
    steps: list[StepRecord] = field(default_factory=list)

    @property
    def duration_s(self) -> float | None:
        if self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.duration_s,
            "end_reason": self.end_reason.value if self.end_reason else None,
            "step_count": self.step_count,
            "steps": [
                {
                    "step_id": s.step_id,
                    "name": s.name,
                    "started_at": s.started_at,
                    "finished_at": s.finished_at,
                    "duration_s": s.duration_s,
                    "status": s.status,
                    "error": s.error,
                }
                for s in self.steps
            ],
        }


class TurnBoundary:
    """Process-wide monotonic counter for turn_id (one source of truth).

    Created per-Orchestrator-instance; not shared across processes. If
    you later run multiple Orchestrators that need globally-unique turn
    IDs (multi-worker setup), swap this for a SQLite-backed allocator.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turn_counter = 0

    def next_turn_id(self) -> int:
        with self._lock:
            self._turn_counter += 1
            return self._turn_counter

    @property
    def last_turn_id(self) -> int:
        with self._lock:
            return self._turn_counter


__all__ = [
    "TurnBoundary",
    "TurnRecord",
    "StepRecord",
    "TurnEndReason",
    "StepName",
]
