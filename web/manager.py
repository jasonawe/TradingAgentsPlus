"""Thread-safe, single-active-run coordination for the local web console."""

from __future__ import annotations

import json
import re
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
        (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED)
    )
    _WORKER_ERROR_MESSAGE = "analysis worker failed"

    def __init__(
        self,
        *,
        event_limit: int = 256,
        terminal_ttl: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] | None = None,
        db_path: str | Path | None = None,
        store: SQLiteStore | None = None,
        lifecycle_config: dict[str, Any] | None = None,
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

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return self._active_run_id

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

    def complete_publishing(self, run_id: str, *, signal: str | None, report_id: str) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            if state.record.status is RunStatus.COMPLETED:
                return self._copy_record(state.record)
            if state.record.status is not RunStatus.PUBLISHING:
                raise RuntimeError("run is not publishing")
            state.record.status = RunStatus.COMPLETED
            state.record.finished_at = self._clock()
            state.record.signal = signal
            state.record.report_id = report_id
            self._finish_locked(state)
            self._persist_locked(state.record)
            self._publish_locked(state, EventName.RUN_COMPLETED, {"status": "completed", "signal": signal, "report_id": report_id}, allow_terminal=True)
            return self._copy_record(state.record)

    def publish(
        self, run_id: str, event: EventName | str, payload: dict[str, Any]
    ) -> EventEnvelope | None:
        with self._lock:
            return self._publish_locked(self._state(run_id), EventName(event), payload)

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
            return self._copy_record(self._state(run_id).record)

    def request_cancel(self, run_id: str) -> RunRecord:
        """Set the cooperative cancellation flag without forcing a terminal state."""

        with self._lock:
            state = self._state(run_id)
            if state.record.status not in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PUBLISHING, RunStatus.INTERRUPTED):
                state.cancel_event.set()
                self._persist_locked(state.record)
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
            self._persist_locked(state.record)
            self._publish_locked(
                state,
                EventName.RUN_COMPLETED,
                {"status": "completed", "signal": signal, "report_id": report_id},
                allow_terminal=True,
            )
            return self._copy_record(state.record)

    def fail_run(self, run_id: str, *, error_code: str, error_message: str) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            if state.record.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
                return self._copy_record(state.record)
            state.record.status = RunStatus.FAILED
            state.record.finished_at = self._clock()
            state.record.terminal_reason = self._sanitize_code(error_code)
            state.record.error_code = state.record.terminal_reason
            state.record.error_message = self._sanitize_error(error_message)
            self._finish_locked(state)
            self._persist_locked(state.record)
            self._publish_locked(
                state,
                EventName.RUN_FAILED,
                {
                    "status": "failed",
                    "error_code": state.record.error_code,
                    "error_message": state.record.error_message,
                },
                allow_terminal=True,
            )
            return self._copy_record(state.record)

    def cancel_run(
        self, run_id: str, *, phase: str | None = None, current_agent: str | None = None
    ) -> RunRecord:
        with self._lock:
            state = self._state(run_id)
            if state.record.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED, RunStatus.PUBLISHING):
                return self._copy_record(state.record)
            state.cancel_event.set()
            state.record.status = RunStatus.CANCELLED
            state.record.finished_at = self._clock()
            if phase is not None:
                state.record.phase = phase
            if current_agent is not None:
                state.record.current_agent = current_agent
            self._finish_locked(state)
            self._persist_locked(state.record)
            self._publish_locked(
                state,
                EventName.RUN_CANCELLED,
                {
                    "status": "cancelled",
                    "phase": state.record.phase or "",
                    "current_agent": state.record.current_agent,
                },
                allow_terminal=True,
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
            terminal = state.record.status in self._TERMINAL_STATUSES
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
        interrupted: list[RunRecord] = []
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
            if record.status in (RunStatus.QUEUED, RunStatus.RUNNING):
                interrupted.append(record)
        if interrupted:
            now = self._clock()
            with self._lock:
                for record in interrupted:
                    state = self._records[record.run_id]
                    state.record.status = RunStatus.INTERRUPTED
                    state.record.finished_at = now
                    state.record.terminal_reason = "service_restart"
                    state.record.error_code = state.record.terminal_reason
                    state.record.error_message = "analysis interrupted by web service restart"
                    state.terminal_expires_at = now + self.terminal_ttl
                    self._persist_locked(state.record)
                    self._publish_locked(state, EventName.RUN_INTERRUPTED, {"status": "interrupted", "error_code": "service_restart", "error_message": state.record.error_message}, allow_terminal=True)

    def _persist_locked(self, record: RunRecord) -> None:
        if self._db_path is None:
            return
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
        connector = self._store.connection() if self._store is not None else sqlite3.connect(self._db_path)
        with connector as connection:
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
