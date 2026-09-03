"""APScheduler-backed orchestration for persisted scheduled analyses."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone as datetime_timezone
from typing import Any

from apscheduler.events import EVENT_JOB_SUBMITTED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import SchedulerNotRunningError
from apscheduler.triggers.cron import CronTrigger

from .manager import AssetBusyError, MaxConcurrentRunsError, RunManager
from .models import AnalysisRequest, RunStatus
from .repositories import (
    AnalysisRunRepository,
    ScheduledJobRepository,
    ScheduledRunLogRepository,
    SettingsRepository,
    WatchlistRepository,
)
from .scheduled import infer_scheduled_request, next_fire_times


class ScheduledAnalysisService:
    """Own job registration, direct triggering, and run-log reconciliation."""

    _TERMINAL_STATUSES = frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
            RunStatus.TIMED_OUT,
        }
    )

    def __init__(
        self,
        *,
        jobs: ScheduledJobRepository,
        logs: ScheduledRunLogRepository,
        runs: AnalysisRunRepository,
        watchlist: WatchlistRepository,
        settings: SettingsRepository,
        manager: RunManager,
        worker: Callable[[str], Any],
        normalize_request: Callable[[AnalysisRequest], AnalysisRequest],
        config: dict[str, Any],
        scheduler: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        timezone: Any = None,
    ) -> None:
        self.jobs = jobs
        self.logs = logs
        self.runs = runs
        self.watchlist = watchlist
        self.settings = settings
        self.manager = manager
        self.worker = worker
        self.normalize_request = normalize_request
        self.config = config
        self.timezone = (
            timezone or datetime.now().astimezone().tzinfo or datetime_timezone.utc
        )
        self.scheduler = scheduler or BackgroundScheduler(timezone=self.timezone)
        self.clock = clock or (lambda: datetime.now(self.timezone))
        self._lock = threading.RLock()
        self._reconcile_threads: set[threading.Thread] = set()
        self._reconcile_stop = threading.Event()
        self._submission_condition = threading.Condition()
        self._submission_times: dict[str, deque[datetime]] = defaultdict(deque)
        self._started = False
        self._stopped = False
        if hasattr(self.scheduler, "add_listener"):
            self.scheduler.add_listener(
                self._capture_scheduled_run_time, EVENT_JOB_SUBMITTED
            )

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self.scheduler.start(paused=True)
            self._started = True
            self._stopped = False
            self._reconcile_stop.clear()
            self.resync()
            self._reconcile_incomplete_logs()
            self.scheduler.resume()

    def shutdown(self) -> None:
        with self._lock:
            if not self._started or self._stopped:
                return
            self._stopped = True
            self._started = False
            self._reconcile_stop.set()
            threads = list(self._reconcile_threads)
        with self._submission_condition:
            self._submission_condition.notify_all()
        with suppress(SchedulerNotRunningError):
            self.scheduler.shutdown(wait=True)
        for thread in threads:
            thread.join(timeout=2.0)

    def resync(self) -> None:
        with self._lock:
            self.scheduler.remove_all_jobs()
            for job in self.jobs.list(enabled=True):
                trigger = CronTrigger.from_crontab(
                    job["cron_expression"], timezone=self.timezone
                )
                self.scheduler.add_job(
                    self._run_registered_job,
                    trigger,
                    args=[job["id"]],
                    id=job["id"],
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=1,
                )

    def _capture_scheduled_run_time(self, event: Any) -> None:
        with self._submission_condition:
            self._submission_times[event.job_id].extend(event.scheduled_run_times)
            self._submission_condition.notify_all()

    def _run_registered_job(self, job_id: str) -> dict[str, Any] | None:
        with self._submission_condition:
            self._submission_condition.wait_for(
                lambda: bool(self._submission_times[job_id])
                or self._reconcile_stop.is_set()
            )
            scheduled_for = (
                self._submission_times[job_id].popleft()
                if self._submission_times[job_id]
                else None
            )
        if self._reconcile_stop.is_set():
            return None
        return self.trigger(job_id, scheduled_for=scheduled_for)

    def preview(
        self, expression: str, *, count: int = 3, now: datetime | None = None
    ) -> list[str]:
        return next_fire_times(
            expression, count=count, now=now, timezone=self.timezone
        )

    def serialize_job(self, job: dict[str, Any]) -> dict[str, Any]:
        scheduled = self.scheduler.get_job(job["id"])
        next_run_time = getattr(scheduled, "next_run_time", None)
        latest = self.logs.list(job["id"], limit=1)
        last = latest[0] if latest else None
        return {
            **job,
            "next_run_at": next_run_time.isoformat() if next_run_time else None,
            "last_run_at": last["fired_at"] if last else None,
            "last_run_status": last["status"] if last else None,
            "last_report_id": last["report_id"] if last else None,
        }

    def list_jobs(self) -> dict[str, Any]:
        jobs = self.jobs.list()
        version_payload = [
            (job["id"], job["updated_at"], job["enabled"]) for job in jobs
        ]
        version = hashlib.sha256(
            json.dumps(version_payload, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return {"items": [self.serialize_job(job) for job in jobs], "version": version}

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self.serialize_job(self.jobs.get(job_id))

    def run_now(self, job_id: str) -> dict[str, Any]:
        return self.trigger(job_id, manual=True)

    def trigger(
        self,
        job_id: str,
        *,
        scheduled_for: datetime | str | None = None,
        manual: bool = False,
    ) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if not manual:
            if not self.settings.scheduler_settings()["enabled"] or not job["enabled"]:
                return None
            job = self.jobs.get(job_id)
            if not job["enabled"]:
                return None

        fired_at = self._aware_now()
        target = self._iso(scheduled_for or fired_at)
        if not self.watchlist.contains(job["symbol"], job["asset_type"]):
            return self._skip(job, target, fired_at, "watchlist_missing")

        try:
            latest = self.runs.latest_successful_request(
                job["symbol"], job["asset_type"]
            )
            request, source = infer_scheduled_request(
                job["symbol"],
                job["asset_type"],
                latest_request=latest,
                config=self.config,
                settings=self.settings.all(),
                today=fired_at.date(),
            )
            request = self.normalize_request(request)
        except (TypeError, ValueError):
            return self._skip(
                job,
                target,
                fired_at,
                "invalid_parameters",
                error="invalid analysis configuration",
            )
        if not manual:
            job = self.jobs.get(job_id)
            if not self.settings.scheduler_settings()["enabled"] or not job["enabled"]:
                return None
        if any(
            active.request.ticker == request.ticker
            and active.request.asset_type == request.asset_type
            for active in self.manager.list_active_runs()
        ):
            return self._skip(
                job, target, fired_at, "asset_busy", parameter_source=source
            )

        gate = threading.Event()
        log_ready = threading.Event()
        log_reference: dict[str, str] = {}

        def scheduled_worker(run_id: str) -> Any:
            gate.wait()
            log_ready.wait()
            with suppress(KeyError, RuntimeError):
                self.logs.update(log_reference["id"], status="running")
            return self.worker(run_id)

        try:
            record = self.manager.start_run(request, worker=scheduled_worker)
        except AssetBusyError:
            return self._skip(
                job, target, fired_at, "asset_busy", parameter_source=source
            )
        except MaxConcurrentRunsError:
            return self._skip(job, target, fired_at, "capacity", parameter_source=source)

        try:
            log = self.logs.create(
                job["id"],
                symbol=job["symbol"],
                asset_type=job["asset_type"],
                scheduled_for=target,
                fired_at=fired_at.isoformat(),
                status="queued",
                run_id=record.run_id,
                parameter_source=source,
            )
        except Exception:
            # The run is already admitted. Release both ordering gates so a
            # logging failure cannot strand its worker thread indefinitely.
            log_ready.set()
            gate.set()
            raise
        log_reference["id"] = log["id"]
        log_ready.set()
        reconcile_thread = threading.Thread(
            target=self._reconcile_run_tracked,
            args=(log["id"], record.run_id),
            name=f"scheduled-reconcile-{record.run_id}",
            daemon=True,
        )
        with self._lock:
            self._reconcile_threads.add(reconcile_thread)
        reconcile_thread.start()
        gate.set()
        return log

    def _reconcile_run_tracked(self, log_id: str, run_id: str) -> None:
        try:
            self._reconcile_run(log_id, run_id)
        finally:
            with self._lock:
                self._reconcile_threads.discard(threading.current_thread())

    def _reconcile_incomplete_logs(self) -> None:
        """Resume or close logs left behind by a previous process instance."""
        for log in self.logs.list_incomplete():
            run_id = log.get("run_id")
            if not run_id:
                with suppress(KeyError, RuntimeError):
                    self.logs.update(log["id"], status="failed", error="run unavailable")
                continue
            try:
                record = self.manager.get_run(run_id)
            except KeyError:
                with suppress(KeyError, RuntimeError):
                    self.logs.update(log["id"], status="failed", error="run unavailable")
                continue
            if record.status in self._TERMINAL_STATUSES:
                status = "succeeded" if record.status is RunStatus.COMPLETED else "failed"
                error = None if status == "succeeded" else (
                    record.error_message or record.terminal_reason or record.status.value
                )
                with suppress(KeyError, RuntimeError):
                    self.logs.update(log["id"], status=status, error=error)
                continue
            reconcile_thread = threading.Thread(
                target=self._reconcile_run_tracked,
                args=(log["id"], run_id),
                name=f"scheduled-reconcile-{run_id}",
                daemon=True,
            )
            with self._lock:
                self._reconcile_threads.add(reconcile_thread)
            reconcile_thread.start()

    def _reconcile_run(self, log_id: str, run_id: str) -> None:
        cursor = 0
        while not self._reconcile_stop.is_set():
            try:
                record = self.manager.get_run(run_id)
            except KeyError:
                with suppress(KeyError, RuntimeError):
                    self.logs.update(log_id, status="failed", error="run unavailable")
                return
            if record.status in self._TERMINAL_STATUSES:
                status = "succeeded" if record.status is RunStatus.COMPLETED else "failed"
                error = None
                if status == "failed":
                    error = (
                        record.error_message
                        or record.terminal_reason
                        or record.status.value
                    )
                with suppress(KeyError, RuntimeError):
                    self.logs.update(log_id, status=status, error=error)
                return
            batch = self.manager.wait_for_events(run_id, cursor, timeout=0.1)
            if batch.events:
                cursor = max(cursor, batch.next_cursor)

    def _skip(
        self,
        job: dict[str, Any],
        scheduled_for: str,
        fired_at: datetime,
        reason: str,
        *,
        error: str | None = None,
        parameter_source: str | None = None,
    ) -> dict[str, Any]:
        return self.logs.create(
            job["id"],
            symbol=job["symbol"],
            asset_type=job["asset_type"],
            scheduled_for=scheduled_for,
            fired_at=fired_at.isoformat(),
            status="skipped",
            skip_reason=reason,
            error=error,
            parameter_source=parameter_source,
        )

    def _aware_now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=self.timezone)
        return value.astimezone(self.timezone)

    def _iso(self, value: datetime | str) -> str:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=self.timezone)
        return parsed.astimezone(self.timezone).isoformat()


__all__ = ["ScheduledAnalysisService"]
