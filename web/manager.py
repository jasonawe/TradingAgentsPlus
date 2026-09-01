"""Thread-safe, single-active-run coordination for the local web console."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from web.storage import SQLiteStore

from web.config import _parse_integer_setting
from web.models import (
    AnalysisRequest,
    EventEnvelope,
    EventName,
    RunRecord,
    RunSnapshotPayload,
    RunStatus,
)
from web.repositories import ReportRepository


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

    _TERMINAL_STATUSES = frozenset(
        (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
            RunStatus.TIMED_OUT,
        )
    )
    _WORKER_ERROR_MESSAGE = "analysis worker failed"
    _TERMINAL_EVENT_BY_STATUS = {
        RunStatus.COMPLETED: EventName.RUN_COMPLETED,
        RunStatus.FAILED: EventName.RUN_FAILED,
        RunStatus.CANCELLED: EventName.RUN_CANCELLED,
        RunStatus.INTERRUPTED: EventName.RUN_INTERRUPTED,
        RunStatus.TIMED_OUT: EventName.RUN_TIMED_OUT,
    }

    def __init__(
        self,
        *,
        event_limit: int = 256,
        terminal_ttl: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] | None = None,
        db_path: str | Path | None = None,
        store: SQLiteStore | None = None,
        lifecycle_config: dict[str, Any] | None = None,
        report_root: str | Path | None = None,
        watchdog_interval: float | None = None,
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
        self._report_root = Path(report_root) if report_root is not None else None
        self._watchdog_interval_override = watchdog_interval
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._shutdown = False
        self.lifecycle_config: dict[str, dict[str, int | str]] = {}
        self.configure_lifecycle(lifecycle_config or {
            "run_timeout_seconds": {"value": 7200, "source": "hard_fallback"},
            "run_heartbeat_interval_seconds": {"value": 15, "source": "hard_fallback"},
            "run_heartbeat_timeout_seconds": {"value": 180, "source": "hard_fallback"},
        })
        self._db_path = Path(db_path) if db_path is not None else None
        self._store = store
        if self._store is None and self._db_path is not None:
            from web.storage import SQLiteStore
            self._store = SQLiteStore(self._db_path)
        if self._db_path is not None and self._store is None:
            self._init_db()
            self._load_persisted_runs()
        elif self._store is not None:
            self._db_path = self._store.path
            self._load_persisted_runs()
        self._start_watchdog()

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            self._check_all_expired_locked()
            return self._active_run_id

    def set_report_root(self, report_root: str | Path) -> None:
        with self._lock:
            self._report_root = Path(report_root)

    def configure_lifecycle(self, lifecycle_config: dict[str, Any]) -> None:
        """Set validated values used by subsequently created runs."""

        ranges = {
            "run_timeout_seconds": (300, 86400),
            "run_heartbeat_interval_seconds": (5, 60),
            "run_heartbeat_timeout_seconds": (30, 600),
        }
        normalized: dict[str, dict[str, int | str]] = {}
        for key, (minimum, maximum) in ranges.items():
            item = lifecycle_config[key]
            if isinstance(item, dict):
                value = _parse_integer_setting(key, item["value"])
                source = str(item.get("source") or "configured")
            else:
                value = _parse_integer_setting(key, item)
                source = "configured"
            if value < minimum or value > maximum:
                raise ValueError(f"invalid {key}: expected {minimum}..{maximum}")
            normalized[key] = {"value": value, "source": source}
        self.lifecycle_config = normalized

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
            now = self._clock()
            run_timeout = int(self.lifecycle_config["run_timeout_seconds"]["value"])
            heartbeat_interval = int(
                self.lifecycle_config["run_heartbeat_interval_seconds"]["value"]
            )
            heartbeat_timeout = int(
                self.lifecycle_config["run_heartbeat_timeout_seconds"]["value"]
            )
            record = RunRecord(
                run_id=identifier,
                request=request,
                queued_at=now,
                last_heartbeat_at=now,
                timeout_at=now + timedelta(seconds=run_timeout),
                run_timeout_seconds=run_timeout,
                run_heartbeat_interval_seconds=heartbeat_interval,
                run_heartbeat_timeout_seconds=heartbeat_timeout,
            )
            record.effective_quote_strategy_id = request.quote_strategy_id
            record.effective_quote_provider_chain = ["yfinance", "alpha_vantage"] if request.quote_strategy_id == "fallback-yfinance-alpha-vantage" else (["yfinance"] if request.quote_strategy_id else [])
            state = _ManagedRun(
                record=record,
                events=deque(maxlen=self.event_limit),
                condition=threading.Condition(self._lock),
                cancel_event=threading.Event(),
            )
            self._records[identifier] = state
            self._active_run_id = identifier
            self._persist_locked(record)
            if worker is not None:
                state.future = self._executor.submit(self._run_worker, identifier, worker)
            return self._copy_record(record)

    def _run_worker(self, run_id: str, worker: Callable[[str], Any]) -> Any:
        try:
            record = self.begin_run(run_id)
            if record.status is not RunStatus.RUNNING:
                return None
            return worker(run_id)
        except Exception:  # pragma: no cover - defensive worker boundary
            # Run records and browser events must never expose provider payloads
            # or credentials from worker exceptions.
            self.fail_run(
                run_id,
                error_code="worker_error",
                error_message=self._WORKER_ERROR_MESSAGE,
            )
            return None

    def begin_run(self, run_id: str) -> RunRecord:
        """Atomically transition a queued run to running and publish its event."""

        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            if state.record.status is RunStatus.RUNNING:
                return self._copy_record(state.record)
            if state.record.status is not RunStatus.QUEUED:
                return self._copy_record(state.record)
            state.record.status = RunStatus.RUNNING
            state.record.started_at = self._clock()
            self._persist_locked(state.record)
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

    def begin_publishing(self, run_id: str) -> RunRecord:
        """CAS running -> publishing; publishing is deliberately not cancellable."""
        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            if state.record.status is RunStatus.PUBLISHING:
                return self._copy_record(state.record)
            if state.record.status is not RunStatus.RUNNING:
                raise RuntimeError("run is not running")
            state.record.status = RunStatus.PUBLISHING
            self._persist_locked(state.record)
            return self._copy_record(state.record)

    def set_data_metadata(
        self,
        run_id: str,
        *,
        data_snapshot_id: str | None = None,
        data_status: str | None = None,
        reproducibility: str | None = None,
    ) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            if data_snapshot_id is not None:
                state.record.data_snapshot_id = data_snapshot_id
            if data_status is not None:
                state.record.data_status = data_status
            if reproducibility is not None:
                state.record.reproducibility = reproducibility
            self._persist_locked(state.record)
            return self._copy_record(state.record)

    def complete_publishing(
        self,
        run_id: str,
        *,
        signal: str | None,
        report_id: str,
        report_dir: str | Path | None = None,
    ) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            if state.record.status is RunStatus.COMPLETED:
                return self._copy_record(state.record)
            if state.record.status in self._TERMINAL_STATUSES:
                return self._copy_record(state.record)
            if state.record.status is not RunStatus.PUBLISHING:
                raise RuntimeError("run is not publishing")
            resolved_report_dir = Path(report_dir) if report_dir is not None else self._report_dir_for(state.record, report_id)
            if resolved_report_dir is None or not ReportRepository.is_gate_ready(resolved_report_dir):
                return self._transition_terminal_locked(
                    state,
                    allowed={RunStatus.PUBLISHING},
                    status=RunStatus.FAILED,
                    event=EventName.RUN_FAILED,
                    terminal_reason="publish_incomplete",
                    error_message="report publication is incomplete",
                )
            return self._transition_terminal_locked(
                state,
                allowed={RunStatus.PUBLISHING},
                status=RunStatus.COMPLETED,
                event=EventName.RUN_COMPLETED,
                terminal_reason="completed",
                signal=signal,
                report_id=report_id,
            )

    def heartbeat(self, run_id: str) -> RunRecord:
        """Persist a Worker activity checkpoint at the configured interval."""

        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            if state.record.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                return self._copy_record(state.record)
            now = self._clock()
            interval = max(0, state.record.run_heartbeat_interval_seconds or 0)
            previous = state.record.last_heartbeat_at
            if previous is None or (now - previous).total_seconds() >= interval:
                state.record.last_heartbeat_at = now
                self._persist_locked(state.record)
            return self._copy_record(state.record)

    def remaining_deadline(self, run_id: str) -> float:
        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            if state.record.status in self._TERMINAL_STATUSES:
                return 0.0
            if state.record.timeout_at is None:
                return float("inf")
            return max(0.0, (state.record.timeout_at - self._clock()).total_seconds())

    def check_expired(self, run_id: str) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            return self._copy_record(state.record)

    def publish(
        self, run_id: str, event: EventName | str, payload: dict[str, Any]
    ) -> EventEnvelope | None:
        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            return self._publish_locked(state, EventName(event), payload)

    def _publish_locked(
        self,
        state: _ManagedRun,
        event: EventName,
        payload: dict[str, Any],
        *,
        allow_terminal: bool = False,
    ) -> EventEnvelope | None:
        if state.record.status in self._TERMINAL_STATUSES and not allow_terminal:
            return None
        if event is EventName.PROGRESS:
            progress = min(1.0, max(0.0, float(payload.get("progress", 0.0))))
            if progress < state.record.progress:
                return None
            payload = dict(payload)
            payload["progress"] = progress
        envelope = EventEnvelope(
            run_id=state.record.run_id,
            seq=state.next_seq,
            timestamp=self._clock(),
            event=event,
            payload=payload,
        )
        self._update_snapshot_locked(state, envelope)
        self._persist_locked(state.record)
        state.next_seq += 1
        state.events.append(envelope)
        self._persist_event(envelope)
        state.condition.notify_all()
        return envelope

    @staticmethod
    def _update_snapshot_locked(state: _ManagedRun, envelope: EventEnvelope) -> None:
        """Mirror live progress events into the durable run snapshot."""

        payload = envelope.payload
        if envelope.event is EventName.PHASE_CHANGED:
            state.record.phase = payload.phase
        elif envelope.event is EventName.AGENT_STATUS:
            if payload.status == "in_progress":
                state.record.current_agent = payload.agent
        elif envelope.event is EventName.PROGRESS:
            state.record.progress = payload.progress
            state.record.phase = payload.phase
            state.record.current_agent = payload.current_agent

    def _persist_event(self, event: EventEnvelope) -> None:
        if self._db_path is None:
            return
        connector = self._store.connection() if self._store is not None else sqlite3.connect(self._db_path)
        with connector as connection:
            connection.execute(
                "INSERT OR IGNORE INTO web_run_events(run_id,seq,event,timestamp,payload_json) VALUES (?,?,?,?,?)",
                (event.run_id, event.seq, event.event.value, event.timestamp.isoformat(), json.dumps(event.payload.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))),
            )

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            self.cleanup()
            state = self._state(run_id)
            self._check_expired_locked(state)
            return self._copy_record(state.record)

    def request_cancel(self, run_id: str) -> RunRecord:
        """Set the cooperative cancellation flag without forcing a terminal state."""

        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            if (
                state.record.status not in self._TERMINAL_STATUSES
                and state.record.status is not RunStatus.PUBLISHING
            ):
                state.cancel_event.set()
                self._persist_locked(state.record)
            return self._copy_record(state.record)

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            return state.cancel_event.is_set()

    def complete_run(self, run_id: str, *, signal: str | None, report_id: str) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            if state.record.status in self._TERMINAL_STATUSES:
                return self._copy_record(state.record)
            return self._transition_terminal_locked(
                state,
                allowed={RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PUBLISHING},
                status=RunStatus.COMPLETED,
                event=EventName.RUN_COMPLETED,
                terminal_reason="completed",
                signal=signal,
                report_id=report_id,
            )

    def fail_run(self, run_id: str, *, error_code: str, error_message: str) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            if state.record.status in self._TERMINAL_STATUSES:
                return self._copy_record(state.record)
            return self._transition_terminal_locked(
                state,
                allowed={RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PUBLISHING},
                status=RunStatus.FAILED,
                event=EventName.RUN_FAILED,
                terminal_reason=error_code,
                error_message=error_message,
            )

    def cancel_run(
        self, run_id: str, *, phase: str | None = None, current_agent: str | None = None
    ) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            if (
                state.record.status in self._TERMINAL_STATUSES
                or state.record.status is RunStatus.PUBLISHING
            ):
                return self._copy_record(state.record)
            if phase is not None:
                state.record.phase = phase
            state.cancel_event.set()
            return self._transition_terminal_locked(
                state,
                allowed={RunStatus.QUEUED, RunStatus.RUNNING},
                status=RunStatus.CANCELLED,
                event=EventName.RUN_CANCELLED,
                terminal_reason="cancelled",
            )

    def read_events(self, run_id: str, cursor: int = 0) -> EventBatch:
        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            return self._read_locked(state, max(0, cursor))

    def wait_for_events(self, run_id: str, cursor: int = 0, timeout: float = 15.0) -> EventBatch:
        with self._lock:
            state = self._state(run_id)
            self._check_expired_locked(state)
            batch = self._read_locked(state, max(0, cursor))
            if batch.events or batch.terminal:
                return batch
            state.condition.wait(timeout=max(0.0, timeout))
            self._check_expired_locked(state)
            batch = self._read_locked(state, max(0, cursor))
            if not batch.events and not batch.terminal:
                return EventBatch([], timed_out=True)
            return batch

    def _read_locked(self, state: _ManagedRun, cursor: int) -> EventBatch:
        retained = list(state.events)
        if not retained:
            terminal = state.record.status in self._TERMINAL_STATUSES
            return EventBatch([], terminal=terminal)
        oldest = retained[0].seq
        stale = cursor < oldest - 1
        events: list[EventEnvelope] = []
        if stale:
            snapshot_seq = state.next_seq - 1
            events.append(
                EventEnvelope(
                    run_id=state.record.run_id,
                    seq=max(1, snapshot_seq),
                    timestamp=self._clock(),
                    event=EventName.RUN_SNAPSHOT,
                    payload=RunSnapshotPayload(
                        run=self._copy_record(state.record),
                        snapshot_seq=snapshot_seq,
                        replay_from_seq=snapshot_seq + 1,
                    ),
                )
            )
            cursor = snapshot_seq
        events.extend(event for event in retained if event.seq > cursor)
        terminal = state.record.status in self._TERMINAL_STATUSES
        return EventBatch(events, stale=stale, terminal=terminal)

    def cleanup(self, *, now: datetime | None = None) -> None:
        with self._lock:
            current = now or self._clock()
            expired = [
                run_id
                for run_id, state in self._records.items()
                if state.terminal_expires_at is not None and state.terminal_expires_at <= current
            ]
            for run_id in expired:
                self._records.pop(run_id, None)
                self._delete_persisted_locked(run_id)

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._watchdog_stop.set()
            watchdog = self._watchdog_thread
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=5.0)
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def _finish_locked(self, state: _ManagedRun) -> None:
        state.terminal_expires_at = self._clock() + self.terminal_ttl
        if self._active_run_id == state.record.run_id:
            self._active_run_id = None
        state.condition.notify_all()

    def _start_watchdog(self) -> None:
        interval = self._watchdog_interval_override
        if interval is None:
            interval = float(
                self.lifecycle_config["run_heartbeat_interval_seconds"]["value"]
            )
        interval = max(0.01, float(interval))
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            args=(interval,),
            name="tradingagents-run-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _watchdog_loop(self, interval: float) -> None:
        while not self._watchdog_stop.wait(interval):
            try:
                with self._lock:
                    self._check_all_expired_locked()
            except Exception:
                continue

    def _check_all_expired_locked(self) -> None:
        for state in tuple(self._records.values()):
            self._check_expired_locked(state)

    def _check_expired_locked(self, state: _ManagedRun) -> bool:
        if state.record.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
            return False
        now = self._clock()
        reason = None
        message = None
        if state.record.timeout_at is not None and now >= state.record.timeout_at:
            reason = "heartbeat_timeout"
            message = "analysis deadline expired"
        else:
            heartbeat_timeout = state.record.run_heartbeat_timeout_seconds
            heartbeat_at = state.record.last_heartbeat_at
            if (
                heartbeat_timeout is not None
                and heartbeat_at is not None
                and (now - heartbeat_at).total_seconds() >= heartbeat_timeout
            ):
                reason = "heartbeat_timeout"
                message = "analysis heartbeat expired"
        if reason is None:
            return False
        self._transition_terminal_locked(
            state,
            allowed={RunStatus.QUEUED, RunStatus.RUNNING},
            status=RunStatus.TIMED_OUT,
            event=EventName.RUN_TIMED_OUT,
            terminal_reason=reason,
            error_message=message,
        )
        return True

    def _transition_terminal_locked(
        self,
        state: _ManagedRun,
        *,
        allowed: set[RunStatus],
        status: RunStatus,
        event: EventName,
        terminal_reason: str,
        error_message: str | None = None,
        signal: str | None = None,
        report_id: str | None = None,
    ) -> RunRecord:
        if state.record.status in self._TERMINAL_STATUSES:
            return self._copy_record(state.record)
        if state.record.status not in allowed:
            return self._copy_record(state.record)
        state.record.status = status
        state.record.finished_at = self._clock()
        state.record.terminal_reason = self._sanitize_code(terminal_reason)
        state.record.error_code = state.record.terminal_reason
        state.record.error_message = (
            self._sanitize_error(error_message) if error_message is not None else None
        )
        state.record.current_agent = None
        if status is RunStatus.COMPLETED:
            state.record.progress = 1.0
            state.record.signal = signal
            state.record.report_id = report_id
        self._finish_locked(state)
        payload = self._terminal_payload(state.record, event)
        envelope = EventEnvelope(
            run_id=state.record.run_id,
            seq=state.next_seq,
            timestamp=self._clock(),
            event=event,
            payload=payload,
        )
        self._persist_terminal_locked(state.record, envelope)
        state.next_seq += 1
        state.events.append(envelope)
        state.condition.notify_all()
        return self._copy_record(state.record)

    @staticmethod
    def _terminal_payload(record: RunRecord, event: EventName) -> dict[str, Any]:
        if event is EventName.RUN_COMPLETED:
            return {
                "status": "completed",
                "signal": record.signal,
                "report_id": record.report_id,
            }
        if event is EventName.RUN_CANCELLED:
            return {
                "status": "cancelled",
                "phase": record.phase or "",
                "current_agent": None,
            }
        if event is EventName.RUN_INTERRUPTED:
            return {
                "status": "interrupted",
                "error_code": "service_restart",
                "error_message": record.error_message or "analysis interrupted",
            }
        if event is EventName.RUN_TIMED_OUT:
            return {
                "status": "timed_out",
                "progress": record.progress,
                "terminal_reason": record.terminal_reason,
                "error_code": record.error_code,
                "error_message": record.error_message or "analysis heartbeat expired",
            }
        return {
            "status": "failed",
            "error_code": record.error_code or "unknown_error",
            "error_message": record.error_message or "analysis failed",
        }

    def _state(self, run_id: str) -> _ManagedRun:
        try:
            return self._records[run_id]
        except KeyError:
            raise KeyError(f"unknown run: {run_id}") from None

    def _init_db(self) -> None:
        assert self._db_path is not None
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connector = self._store.connection() if self._store is not None else sqlite3.connect(self._db_path)
        with connector as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_runs (
                    run_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT,
                    current_agent TEXT,
                    progress REAL NOT NULL,
                    queued_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    signal TEXT,
                    report_id TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    terminal_expires_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS web_run_events (run_id TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL, timestamp TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(run_id, seq))"
            )

    def _load_persisted_runs(self) -> None:
        assert self._db_path is not None
        connector = self._store.connection() if self._store is not None else sqlite3.connect(self._db_path)
        with connector as connection:
            rows = connection.execute("SELECT * FROM web_runs").fetchall()
        recoverable: list[RunRecord] = []
        for row in rows:
            values = dict(row)
            run_id, request_json, status = values["run_id"], values["request_json"], values["status"]
            phase, current_agent, progress = values.get("phase"), values.get("current_agent"), values.get("progress", 0)
            queued_at, started_at, finished_at = values.get("queued_at"), values.get("started_at"), values.get("finished_at")
            signal, report_id, error_code = values.get("signal"), values.get("report_id"), values.get("error_code")
            error_message, terminal_expires_at = values.get("error_message"), values.get("terminal_expires_at")
            provider_chain = values.get("effective_quote_provider_chain")
            try:
                provider_chain = json.loads(provider_chain) if provider_chain else []
            except (TypeError, ValueError):
                provider_chain = []
            try:
                request = AnalysisRequest.model_validate(json.loads(request_json))
                record = RunRecord(
                    run_id=run_id,
                    request=request,
                    status=RunStatus(status),
                    phase=phase,
                    current_agent=current_agent,
                    progress=progress,
                    queued_at=queued_at,
                    started_at=started_at,
                    finished_at=finished_at,
                    signal=signal,
                    report_id=report_id,
                    error_code=error_code,
                    error_message=error_message,
                    effective_quote_strategy_id=values.get("effective_quote_strategy_id"),
                    effective_quote_provider_chain=provider_chain,
                    data_snapshot_id=values.get("data_snapshot_id"),
                    data_status=values.get("data_status"),
                    reproducibility=values.get("reproducibility"),
                    last_heartbeat_at=values.get("last_heartbeat_at"),
                    timeout_at=values.get("timeout_at"),
                    terminal_reason=values.get("terminal_reason"),
                    run_timeout_seconds=values.get("run_timeout_seconds"),
                    run_heartbeat_interval_seconds=values.get("run_heartbeat_interval_seconds"),
                    run_heartbeat_timeout_seconds=values.get("run_heartbeat_timeout_seconds"),
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            state = _ManagedRun(
                record=record,
                events=deque(maxlen=self.event_limit),
                condition=threading.Condition(self._lock),
                cancel_event=threading.Event(),
                terminal_expires_at=self._parse_datetime(terminal_expires_at),
            )
            connector = self._store.connection() if self._store is not None else sqlite3.connect(self._db_path)
            with connector as connection:
                event_rows = connection.execute("SELECT seq,event,timestamp,payload_json FROM web_run_events WHERE run_id=? ORDER BY seq", (record.run_id,)).fetchall()
            for seq, event_name, timestamp, payload_json in event_rows:
                try:
                    state.events.append(EventEnvelope(run_id=record.run_id, seq=seq, timestamp=self._parse_datetime(timestamp), event=event_name, payload=json.loads(payload_json)))
                    state.next_seq = max(state.next_seq, seq + 1)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            self._records[record.run_id] = state
            if record.status in (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.PUBLISHING):
                recoverable.append(record)
        with self._lock:
            for record in recoverable:
                state = self._records[record.run_id]
                if record.status is RunStatus.PUBLISHING:
                    self._recover_publishing_locked(state)
                else:
                    self._transition_terminal_locked(
                        state,
                        allowed={RunStatus.QUEUED, RunStatus.RUNNING},
                        status=RunStatus.INTERRUPTED,
                        event=EventName.RUN_INTERRUPTED,
                        terminal_reason="service_restart",
                        error_message="analysis interrupted by web service restart",
                    )
            for state in self._records.values():
                self._normalize_loaded_terminal_locked(state)
                self._repair_terminal_event_locked(state)

    def _normalize_loaded_terminal_locked(self, state: _ManagedRun) -> None:
        status = state.record.status
        if status not in self._TERMINAL_STATUSES:
            return
        defaults = {
            RunStatus.COMPLETED: "completed",
            RunStatus.FAILED: "failed",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.INTERRUPTED: "service_restart",
            RunStatus.TIMED_OUT: "heartbeat_timeout",
        }
        if status is RunStatus.FAILED:
            reason = state.record.terminal_reason or state.record.error_code or defaults[status]
        else:
            reason = defaults[status]
        state.record.terminal_reason = self._sanitize_code(reason)
        state.record.error_code = state.record.terminal_reason
        state.record.current_agent = None
        if status is RunStatus.COMPLETED:
            state.record.progress = 1.0
            state.record.report_id = state.record.report_id or state.record.run_id
        if state.record.finished_at is None:
            state.record.finished_at = self._clock()
        if state.terminal_expires_at is None:
            state.terminal_expires_at = self._clock() + self.terminal_ttl
        self._persist_locked(state.record)

    def _recover_publishing_locked(self, state: _ManagedRun) -> None:
        report_id = state.record.report_id or state.record.run_id
        report_dir = self._report_dir_for(state.record, report_id)
        if report_dir is not None and ReportRepository.is_gate_ready(report_dir):
            try:
                metadata = json.loads((report_dir / "run.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                metadata = {}
            self._transition_terminal_locked(
                state,
                allowed={RunStatus.PUBLISHING},
                status=RunStatus.COMPLETED,
                event=EventName.RUN_COMPLETED,
                terminal_reason="completed",
                signal=metadata.get("signal"),
                report_id=str(metadata.get("report_id") or report_id),
            )
            return
        if report_dir is not None and report_dir.exists():
            self._quarantine_report(report_dir, state.record.run_id)
        temporary_dir = self._temporary_report_dir_for(state.record)
        if temporary_dir is not None and temporary_dir.exists():
            self._quarantine_report(temporary_dir, state.record.run_id)
        self._transition_terminal_locked(
            state,
            allowed={RunStatus.PUBLISHING},
            status=RunStatus.FAILED,
            event=EventName.RUN_FAILED,
            terminal_reason="publish_incomplete",
            error_message="report publication is incomplete after service restart",
        )

    def _repair_terminal_event_locked(self, state: _ManagedRun) -> None:
        if state.record.status not in self._TERMINAL_STATUSES:
            return
        expected = self._TERMINAL_EVENT_BY_STATUS[state.record.status]
        if any(event.event is expected for event in state.events):
            return
        event = EventEnvelope(
            run_id=state.record.run_id,
            seq=state.next_seq,
            timestamp=state.record.finished_at or self._clock(),
            event=expected,
            payload=self._terminal_payload(state.record, expected),
        )
        self._persist_terminal_locked(state.record, event)
        state.next_seq += 1
        state.events.append(event)

    def _report_dir_for(
        self, record: RunRecord, report_id: str | None = None
    ) -> Path | None:
        if self._report_root is None:
            return None
        return (
            self._report_root
            / record.request.ticker
            / str(record.request.analysis_date)
            / (report_id or record.run_id)
        )

    def _temporary_report_dir_for(self, record: RunRecord) -> Path | None:
        final_dir = self._report_dir_for(record)
        if final_dir is None:
            return None
        return final_dir.parent / ".tmp" / record.run_id

    def _quarantine_report(self, report_dir: Path, run_id: str) -> None:
        if self._report_root is None:
            return
        orphan_root = self._report_root / ".orphaned"
        orphan_root.mkdir(parents=True, exist_ok=True)
        target = orphan_root / f"{run_id}-{uuid.uuid4().hex[:8]}"
        shutil.move(str(report_dir), str(target))

    def _persist_locked(self, record: RunRecord) -> None:
        if self._db_path is None:
            return
        connector = self._store.connection() if self._store is not None else sqlite3.connect(self._db_path)
        with connector as connection:
            self._upsert_record_connection(connection, record)

    def _upsert_record_connection(
        self, connection: sqlite3.Connection, record: RunRecord
    ) -> None:
        state = self._records.get(record.run_id)
        terminal_expires_at = state.terminal_expires_at.isoformat() if state and state.terminal_expires_at else None
        values = (
            record.run_id,
            json.dumps(record.request.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
            record.status.value,
            record.phase,
            record.current_agent,
            record.progress,
            self._format_datetime(record.queued_at),
            self._format_datetime(record.started_at),
            self._format_datetime(record.finished_at),
            record.signal,
            record.report_id,
            record.error_code,
            record.error_message,
            terminal_expires_at,
            record.effective_quote_strategy_id,
            json.dumps(record.effective_quote_provider_chain, ensure_ascii=False),
            record.data_snapshot_id,
            record.data_status,
            record.reproducibility,
            self._format_datetime(record.last_heartbeat_at),
            self._format_datetime(record.timeout_at),
            record.terminal_reason,
            record.run_timeout_seconds,
            record.run_heartbeat_interval_seconds,
            record.run_heartbeat_timeout_seconds,
        )
        connection.execute(
            """
                INSERT INTO web_runs (
                    run_id, request_json, status, phase, current_agent, progress,
                    queued_at, started_at, finished_at, signal, report_id, error_code,
                    error_message, terminal_expires_at, effective_quote_strategy_id,
                    effective_quote_provider_chain, data_snapshot_id, data_status, reproducibility
                    , last_heartbeat_at, timeout_at, terminal_reason, run_timeout_seconds,
                    run_heartbeat_interval_seconds, run_heartbeat_timeout_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    request_json=excluded.request_json, status=excluded.status,
                    phase=excluded.phase, current_agent=excluded.current_agent,
                    progress=excluded.progress, queued_at=excluded.queued_at,
                    started_at=excluded.started_at, finished_at=excluded.finished_at,
                    signal=excluded.signal, report_id=excluded.report_id,
                    error_code=excluded.error_code, error_message=excluded.error_message,
                    terminal_expires_at=excluded.terminal_expires_at,
                    effective_quote_strategy_id=excluded.effective_quote_strategy_id,
                    effective_quote_provider_chain=excluded.effective_quote_provider_chain,
                    data_snapshot_id=excluded.data_snapshot_id, data_status=excluded.data_status,
                    reproducibility=excluded.reproducibility,
                    last_heartbeat_at=excluded.last_heartbeat_at, timeout_at=excluded.timeout_at,
                    terminal_reason=excluded.terminal_reason,
                    run_timeout_seconds=excluded.run_timeout_seconds,
                    run_heartbeat_interval_seconds=excluded.run_heartbeat_interval_seconds,
                    run_heartbeat_timeout_seconds=excluded.run_heartbeat_timeout_seconds
                """,
            values,
        )

    def _persist_terminal_locked(
        self, record: RunRecord, event: EventEnvelope
    ) -> None:
        if self._db_path is None:
            return
        connector = self._store.connection() if self._store is not None else sqlite3.connect(self._db_path)
        with connector as connection:
            self._upsert_record_connection(connection, record)
            connection.execute(
                "INSERT OR IGNORE INTO web_run_events(run_id,seq,event,timestamp,payload_json) "
                "VALUES (?,?,?,?,?)",
                (
                    event.run_id,
                    event.seq,
                    event.event.value,
                    event.timestamp.isoformat(),
                    json.dumps(
                        event.payload.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            )

    def _delete_persisted_locked(self, run_id: str) -> None:
        if self._db_path is None:
            return
        connector = self._store.connection() if self._store is not None else sqlite3.connect(self._db_path)
        with connector as connection:
            connection.execute("DELETE FROM web_runs WHERE run_id = ?", (run_id,))

    @staticmethod
    def _format_datetime(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

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
