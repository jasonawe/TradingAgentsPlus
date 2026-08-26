"""Thread-safe, single-active-run coordination for the local web console."""

from __future__ import annotations

import re
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from web.models import (
    AnalysisRequest,
    EventEnvelope,
    EventName,
    RunRecord,
    RunSnapshotPayload,
    RunStatus,
)


class ActiveRunError(RuntimeError):
    """Raised when a second run is submitted while one is active."""


@dataclass(frozen=True)
class EventBatch:
    """Events available to one subscriber cursor."""

    events: list[EventEnvelope]
    stale: bool = False
    terminal: bool = False
    timed_out: bool = False

    @property
    def next_cursor(self) -> int:
        return self.events[-1].seq if self.events else 0


@dataclass
class _ManagedRun:
    record: RunRecord
    events: deque[EventEnvelope]
    condition: threading.Condition
    cancel_event: threading.Event
    next_seq: int = 1
    terminal_expires_at: datetime | None = None
    future: Future[Any] | None = None


class RunManager:
    """Own run state, bounded event replay, and the one-worker lifecycle.

    All mutations of a run record occur under ``_lock``. Subscribers only hold
    their own cursor; reading an event batch never consumes events for anyone
    else.
    """

    def __init__(
        self,
        *,
        event_limit: int = 256,
        terminal_ttl: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if event_limit < 1:
            raise ValueError("event_limit must be positive")
        self.event_limit = event_limit
        self.terminal_ttl = terminal_ttl
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tradingagents-web")
        self._records: dict[str, _ManagedRun] = {}
        self._active_run_id: str | None = None

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return self._active_run_id

    def start_run(
        self,
        request: AnalysisRequest,
        worker: Callable[[str], Any] | None = None,
        *,
        run_id: str | None = None,
    ) -> RunRecord:
        """Create a queued run and optionally submit its worker.

        ``worker`` receives the opaque run ID. The worker should use
        ``begin_run`` and one of the terminal methods, or may simply perform
        work because the wrapper begins it automatically.
        """

        with self._lock:
            self.cleanup()
            if self._active_run_id is not None:
                raise ActiveRunError("an analysis run is already active")
            identifier = run_id or f"run-{uuid.uuid4().hex}"
            if identifier in self._records:
                raise ValueError("run_id already exists")
            record = RunRecord(run_id=identifier, request=request, queued_at=self._clock())
            state = _ManagedRun(
                record=record,
                events=deque(maxlen=self.event_limit),
                condition=threading.Condition(self._lock),
                cancel_event=threading.Event(),
            )
            self._records[identifier] = state
            self._active_run_id = identifier
            if worker is not None:
                state.future = self._executor.submit(self._run_worker, identifier, worker)
            return self._copy_record(record)

    def _run_worker(self, run_id: str, worker: Callable[[str], Any]) -> Any:
        try:
            record = self.begin_run(run_id)
            if record.status is not RunStatus.RUNNING:
                return None
            return worker(run_id)
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            self.fail_run(run_id, error_code="worker_error", error_message=self._sanitize_error(exc))
            return None

    def begin_run(self, run_id: str) -> RunRecord:
        """Atomically transition a queued run to running and publish its event."""

        with self._lock:
            state = self._state(run_id)
            if state.record.status is RunStatus.RUNNING:
                return self._copy_record(state.record)
            if state.record.status is not RunStatus.QUEUED:
                return self._copy_record(state.record)
            state.record.status = RunStatus.RUNNING
            state.record.started_at = self._clock()
            self._publish_locked(
                state,
                EventName.RUN_STARTED,
                {
                    "status": "running",
                    "ticker": state.record.request.ticker,
                    "analysis_date": state.record.request.analysis_date,
                    "asset_type": state.record.request.asset_type,
                    "analysts": state.record.request.analysts,
                    "research_depth": state.record.request.research_depth,
                },
            )
            return self._copy_record(state.record)

    def publish(self, run_id: str, event: EventName | str, payload: dict[str, Any]) -> EventEnvelope:
        with self._lock:
            return self._publish_locked(self._state(run_id), EventName(event), payload)

    def _publish_locked(
        self, state: _ManagedRun, event: EventName, payload: dict[str, Any]
    ) -> EventEnvelope:
        envelope = EventEnvelope(
            run_id=state.record.run_id,
            seq=state.next_seq,
            timestamp=self._clock(),
            event=event,
            payload=payload,
        )
        state.next_seq += 1
        state.events.append(envelope)
        state.condition.notify_all()
        return envelope

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            self.cleanup()
            return self._copy_record(self._state(run_id).record)

    def request_cancel(self, run_id: str) -> RunRecord:
        """Set the cooperative cancellation flag without forcing a terminal state."""

        with self._lock:
            state = self._state(run_id)
            if state.record.status not in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
                state.cancel_event.set()
            return self._copy_record(state.record)

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return self._state(run_id).cancel_event.is_set()

    def complete_run(self, run_id: str, *, signal: str | None, report_id: str) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            if state.record.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
                return self._copy_record(state.record)
            state.record.status = RunStatus.COMPLETED
            state.record.finished_at = self._clock()
            state.record.signal = signal
            state.record.report_id = report_id
            self._finish_locked(state)
            self._publish_locked(
                state,
                EventName.RUN_COMPLETED,
                {"status": "completed", "signal": signal, "report_id": report_id},
            )
            return self._copy_record(state.record)

    def fail_run(self, run_id: str, *, error_code: str, error_message: str) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            if state.record.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
                return self._copy_record(state.record)
            state.record.status = RunStatus.FAILED
            state.record.finished_at = self._clock()
            state.record.error_code = self._sanitize_code(error_code)
            state.record.error_message = self._sanitize_error(error_message)
            self._finish_locked(state)
            self._publish_locked(
                state,
                EventName.RUN_FAILED,
                {
                    "status": "failed",
                    "error_code": state.record.error_code,
                    "error_message": state.record.error_message,
                },
            )
            return self._copy_record(state.record)

    def cancel_run(
        self, run_id: str, *, phase: str | None = None, current_agent: str | None = None
    ) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            if state.record.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
                return self._copy_record(state.record)
            state.cancel_event.set()
            state.record.status = RunStatus.CANCELLED
            state.record.finished_at = self._clock()
            if phase is not None:
                state.record.phase = phase
            if current_agent is not None:
                state.record.current_agent = current_agent
            self._finish_locked(state)
            self._publish_locked(
                state,
                EventName.RUN_CANCELLED,
                {
                    "status": "cancelled",
                    "phase": state.record.phase or "",
                    "current_agent": state.record.current_agent,
                },
            )
            return self._copy_record(state.record)

    def read_events(self, run_id: str, cursor: int = 0) -> EventBatch:
        with self._lock:
            return self._read_locked(self._state(run_id), max(0, cursor))

    def wait_for_events(self, run_id: str, cursor: int = 0, timeout: float = 15.0) -> EventBatch:
        with self._lock:
            state = self._state(run_id)
            batch = self._read_locked(state, max(0, cursor))
            if batch.events or batch.terminal:
                return batch
            state.condition.wait(timeout=max(0.0, timeout))
            batch = self._read_locked(state, max(0, cursor))
            if not batch.events and not batch.terminal:
                return EventBatch([], timed_out=True)
            return batch

    def _read_locked(self, state: _ManagedRun, cursor: int) -> EventBatch:
        retained = list(state.events)
        if not retained:
            terminal = state.record.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)
            return EventBatch([], terminal=terminal)
        oldest = retained[0].seq
        stale = cursor < oldest - 1
        events: list[EventEnvelope] = []
        if stale:
            snapshot_seq = max(1, oldest - 1)
            events.append(
                EventEnvelope(
                    run_id=state.record.run_id,
                    seq=snapshot_seq,
                    timestamp=self._clock(),
                    event=EventName.RUN_SNAPSHOT,
                    payload=RunSnapshotPayload(run=self._copy_record(state.record), replay_from_seq=cursor),
                )
            )
        events.extend(event for event in retained if event.seq > cursor)
        terminal = state.record.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)
        return EventBatch(events, stale=stale, terminal=terminal)

    def cleanup(self, *, now: datetime | None = None) -> None:
        current = now or self._clock()
        expired = [
            run_id
            for run_id, state in self._records.items()
            if state.terminal_expires_at is not None and state.terminal_expires_at <= current
        ]
        for run_id in expired:
            self._records.pop(run_id, None)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def _finish_locked(self, state: _ManagedRun) -> None:
        state.terminal_expires_at = self._clock() + self.terminal_ttl
        if self._active_run_id == state.record.run_id:
            self._active_run_id = None
        state.condition.notify_all()

    def _state(self, run_id: str) -> _ManagedRun:
        try:
            return self._records[run_id]
        except KeyError:
            raise KeyError(f"unknown run: {run_id}") from None

    @staticmethod
    def _copy_record(record: RunRecord) -> RunRecord:
        return record.model_copy(deep=True)

    @staticmethod
    def _sanitize_code(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", str(value))[:64] or "unknown_error"

    @staticmethod
    def _sanitize_error(value: Any) -> str:
        text = str(value).replace("\r", " ").replace("\n", " ")
        return re.sub(r"\s+", " ", text)[:500]
