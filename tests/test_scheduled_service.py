import time
from datetime import date, datetime, timedelta, timezone

import pytest
from apscheduler.triggers.date import DateTrigger

from web.manager import RunManager
from web.models import AnalysisRequest
from web.repositories import (
    AnalysisRunRepository,
    ScheduledJobRepository,
    ScheduledRunLogRepository,
    SettingsRepository,
    WatchlistRepository,
)
from web.scheduler import ScheduledAnalysisService
from web.storage import SQLiteStore


class FakeScheduler:
    def __init__(self):
        self.running = False
        self.start_calls = 0
        self.shutdown_calls = 0
        self.jobs = {}

    def start(self, paused=False):
        self.running = True
        self.start_calls += 1

    def resume(self):
        self.running = True

    def shutdown(self, wait=True):
        self.running = False
        self.shutdown_calls += 1

    def remove_all_jobs(self):
        self.jobs.clear()

    def add_job(self, func, trigger, **kwargs):
        job = type(
            "FakeJob",
            (),
            {
                "id": kwargs["id"],
                "next_run_time": trigger.get_next_fire_time(
                    None, datetime.now(trigger.timezone)
                ),
            },
        )()
        self.jobs[job.id] = {"job": job, "func": func, "trigger": trigger, **kwargs}
        return job

    def get_job(self, job_id):
        registered = self.jobs.get(job_id)
        return registered["job"] if registered else None


def _request(ticker="AAPL"):
    return AnalysisRequest(
        ticker=ticker,
        analysis_date=date(2026, 9, 2),
        asset_type="stock",
        analysts=["market", "news"],
        research_depth=1,
        output_language="English",
    )


def _service(
    tmp_path, *, worker=None, normalizer=None, clock=None, real_scheduler=False
):
    store = SQLiteStore(tmp_path / "scheduled-service.sqlite3")
    settings = SettingsRepository(store)
    manager = RunManager(store=store)
    manager.configure_concurrency(settings.all())
    scheduler = None if real_scheduler else FakeScheduler()
    service = ScheduledAnalysisService(
        jobs=ScheduledJobRepository(store),
        logs=ScheduledRunLogRepository(store),
        runs=AnalysisRunRepository(store),
        watchlist=WatchlistRepository(store),
        settings=settings,
        manager=manager,
        worker=worker or (lambda run_id: manager.complete_run(run_id, signal="BUY", report_id=run_id)),
        normalize_request=normalizer or (lambda request: request),
        config={},
        scheduler=scheduler,
        clock=clock,
        timezone=timezone.utc,
    )
    return service, store, manager, service.scheduler


def _wait_for_log(service, job_id, expected, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = service.logs.list(job_id)
        if logs and logs[0]["status"] == expected:
            return logs[0]
        time.sleep(0.01)
    assert service.logs.list(job_id)[0]["status"] == expected


def test_scheduler_settings_are_persisted_canonically_and_validated(tmp_path):
    store = SQLiteStore(tmp_path / "settings.sqlite3")
    repo = SettingsRepository(store)

    assert repo.scheduler_settings() == {"enabled": True, "max_concurrent_runs": 3}
    assert repo.get("scheduler.enabled") == {"value": "true", "source": "default"}
    assert repo.get("scheduler.max_concurrent_runs") == {
        "value": "3",
        "source": "default",
    }

    assert repo.update_scheduler_settings(enabled=False, max_concurrent_runs="07") == {
        "enabled": False,
        "max_concurrent_runs": 7,
    }
    assert repo.get("scheduler.enabled")["value"] == "false"
    assert repo.get("scheduler.max_concurrent_runs")["value"] == "7"
    with pytest.raises(ValueError, match="1..10"):
        repo.update_scheduler_settings(max_concurrent_runs=11)
    with pytest.raises(ValueError, match="boolean"):
        repo.update_scheduler_settings(enabled="false")
    store.close()


def test_start_resync_and_shutdown_are_idempotent_and_register_only_enabled_jobs(tmp_path):
    service, store, manager, scheduler = _service(tmp_path)
    enabled = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")
    disabled = service.jobs.create(
        "MSFT", asset_type="stock", cron_expression="30 9 * * *", enabled=False
    )

    service.start()
    service.start()
    assert scheduler.start_calls == 1
    assert set(scheduler.jobs) == {enabled["id"]}
    registered = scheduler.jobs[enabled["id"]]
    assert registered["coalesce"] is True
    assert registered["max_instances"] == 1
    assert registered["misfire_grace_time"] == 1
    assert "next_run_time" not in registered
    assert service.logs.list() == []

    service.jobs.toggle(disabled["id"], True)
    service.resync()
    service.resync()
    assert set(scheduler.jobs) == {enabled["id"], disabled["id"]}
    service.shutdown()
    service.shutdown()
    assert scheduler.shutdown_calls == 1
    manager.shutdown()
    store.close()


def test_real_scheduler_records_the_nominal_fire_time(tmp_path):
    service, store, manager, scheduler = _service(tmp_path, real_scheduler=True)
    service.watchlist.add_item("AAPL", asset_type="stock")
    job = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")
    service.start()
    scheduled_for = datetime.now(timezone.utc) + timedelta(milliseconds=250)
    scheduler.add_job(
        service._run_registered_job,
        DateTrigger(run_date=scheduled_for),
        args=[job["id"]],
        id=job["id"],
        replace_existing=True,
    )

    log = _wait_for_log(service, job["id"], "succeeded")
    assert datetime.fromisoformat(log["scheduled_for"]) == scheduled_for
    assert datetime.fromisoformat(log["fired_at"]) >= scheduled_for

    service.shutdown()
    manager.shutdown()
    store.close()


def test_job_serialization_includes_next_and_latest_log_fields(tmp_path):
    now = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)
    service, store, manager, _scheduler = _service(tmp_path, clock=lambda: now)
    job = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")
    service.logs.create(
        job["id"],
        symbol="AAPL",
        asset_type="stock",
        scheduled_for=now.isoformat(),
        fired_at=now.isoformat(),
        status="skipped",
        skip_reason="capacity",
    )
    service.start()

    item = service.serialize_job(service.jobs.get(job["id"]))
    assert item["next_run_at"] is not None
    assert item["last_run_at"] == now.isoformat()
    assert item["last_run_status"] == "skipped"
    assert service.preview("0 9 * * *", count=2, now=now) == [
        "2026-09-02T09:00:00+00:00",
        "2026-09-03T09:00:00+00:00",
    ]
    manager.shutdown()
    store.close()


def test_scheduled_switches_noop_but_run_now_bypasses_both(tmp_path):
    service, store, manager, _scheduler = _service(tmp_path)
    service.watchlist.add_item("AAPL", asset_type="stock")
    job = service.jobs.create(
        "AAPL", asset_type="stock", cron_expression="0 9 * * *", enabled=False
    )
    service.settings.update_scheduler_settings(enabled=False)

    assert service.trigger(job["id"]) is None
    assert service.logs.list(job["id"]) == []
    log = service.run_now(job["id"])
    assert log["status"] in {"queued", "running", "succeeded"}
    assert _wait_for_log(service, job["id"], "succeeded")["run_id"]
    manager.shutdown()
    store.close()


def test_trigger_records_queued_then_running_before_terminal(tmp_path):
    worker_started = __import__("threading").Event()
    release = __import__("threading").Event()
    service = None

    def worker(run_id):
        worker_started.set()
        release.wait(2)
        service.manager.complete_run(run_id, signal="BUY", report_id=run_id)

    service, store, manager, _scheduler = _service(tmp_path, worker=worker)
    service.watchlist.add_item("AAPL", asset_type="stock")
    job = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")

    created = service.trigger(job["id"])
    assert created["status"] == "queued"
    assert worker_started.wait(1)
    assert _wait_for_log(service, job["id"], "running")["run_id"] == created["run_id"]
    release.set()
    assert _wait_for_log(service, job["id"], "succeeded")["run_id"] == created["run_id"]
    manager.shutdown()
    store.close()


def test_scheduled_trigger_rechecks_master_switch_before_submission(tmp_path):
    service = None

    def disable_scheduler(request):
        service.settings.update_scheduler_settings(enabled=False)
        return request

    service, store, manager, _scheduler = _service(
        tmp_path, normalizer=disable_scheduler
    )
    service.watchlist.add_item("AAPL", asset_type="stock")
    job = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")

    assert service.trigger(job["id"]) is None
    assert service.logs.list(job["id"]) == []
    assert manager.list_active_runs() == []
    manager.shutdown()
    store.close()


def test_shutdown_stops_reconciliation_without_waiting_for_active_run(tmp_path):
    import threading

    release = threading.Event()
    service, store, manager, _scheduler = _service(
        tmp_path, worker=lambda _run_id: release.wait(5)
    )
    service.watchlist.add_item("AAPL", asset_type="stock")
    job = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")
    service.start()
    service.trigger(job["id"])

    started_at = time.monotonic()
    service.shutdown()

    assert time.monotonic() - started_at < 1
    assert service._reconcile_threads == set()
    release.set()
    manager.shutdown()
    store.close()


def test_missing_log_does_not_prevent_the_admitted_worker_from_running(
    tmp_path, monkeypatch
):
    worker_ran = __import__("threading").Event()
    service = None

    def worker(run_id):
        worker_ran.set()
        service.manager.complete_run(run_id, signal="BUY", report_id=run_id)

    service, store, manager, _scheduler = _service(tmp_path, worker=worker)
    service.watchlist.add_item("AAPL", asset_type="stock")
    job = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")
    original_update = service.logs.update

    def missing_running_log(log_id, **kwargs):
        if kwargs.get("status") == "running":
            raise KeyError(log_id)
        return original_update(log_id, **kwargs)

    monkeypatch.setattr(service.logs, "update", missing_running_log)
    service.trigger(job["id"])

    assert worker_ran.wait(1)
    manager.shutdown()
    store.close()


def test_log_creation_failure_releases_the_admitted_worker(tmp_path, monkeypatch):
    worker_ran = __import__("threading").Event()
    service = None

    def worker(run_id):
        worker_ran.set()
        service.manager.complete_run(run_id, signal="BUY", report_id=run_id)

    service, store, manager, _scheduler = _service(tmp_path, worker=worker)
    service.watchlist.add_item("AAPL", asset_type="stock")
    job = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")

    def fail_create(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service.logs, "create", fail_create)
    with pytest.raises(RuntimeError, match="database unavailable"):
        service.trigger(job["id"])

    assert worker_ran.wait(1)
    manager.shutdown()
    store.close()


def test_trigger_logs_watchlist_missing_asset_busy_capacity_and_invalid_parameters(tmp_path):
    service, store, manager, _scheduler = _service(tmp_path)
    missing = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")
    assert service.trigger(missing["id"])["skip_reason"] == "watchlist_missing"

    service.watchlist.add_item("MSFT", asset_type="stock")
    busy = service.jobs.create("MSFT", asset_type="stock", cron_expression="0 9 * * *")
    manager.start_run(_request("MSFT"), worker=lambda _run_id: time.sleep(1))
    assert service.trigger(busy["id"])["skip_reason"] == "asset_busy"

    service.watchlist.add_item("NVDA", asset_type="stock")
    full = service.jobs.create("NVDA", asset_type="stock", cron_expression="0 9 * * *")
    manager.configure_concurrency(
        {"scheduler.max_concurrent_runs": {"value": 1, "source": "test"}}
    )
    assert service.trigger(full["id"])["skip_reason"] == "capacity"
    manager.shutdown(wait=False)
    store.close()


def test_asset_busy_takes_precedence_when_the_manager_is_also_at_capacity(tmp_path):
    service, store, manager, _scheduler = _service(tmp_path)
    service.watchlist.add_item("AAPL", asset_type="stock")
    job = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")
    manager.configure_concurrency(
        {"scheduler.max_concurrent_runs": {"value": 1, "source": "test"}}
    )
    manager.start_run(_request("AAPL"), worker=lambda _run_id: time.sleep(1))

    assert service.trigger(job["id"])["skip_reason"] == "asset_busy"
    manager.shutdown(wait=False)
    store.close()

    def invalid(_request):
        raise ValueError("secret-key=must-not-leak")

    service, store, manager, _scheduler = _service(tmp_path / "invalid", normalizer=invalid)
    service.watchlist.add_item("AAPL", asset_type="stock")
    job = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")
    log = service.trigger(job["id"])
    assert log["skip_reason"] == "invalid_parameters"
    assert log["error"] == "invalid analysis configuration"
    manager.shutdown()
    store.close()


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [("completed", "succeeded"), ("failed", "failed"), ("cancelled", "failed")],
)
def test_trigger_reconciles_terminal_run_status(tmp_path, terminal, expected):
    service = None

    def worker(run_id):
        if terminal == "completed":
            service.manager.complete_run(run_id, signal="BUY", report_id=run_id)
        elif terminal == "failed":
            service.manager.fail_run(
                run_id, error_code="worker_error", error_message="safe failure"
            )
        else:
            service.manager.cancel_run(run_id)

    service, store, manager, _scheduler = _service(tmp_path, worker=worker)
    service.watchlist.add_item("AAPL", asset_type="stock")
    job = service.jobs.create("AAPL", asset_type="stock", cron_expression="0 9 * * *")

    created = service.trigger(job["id"])
    final = _wait_for_log(service, job["id"], expected)
    assert final["run_id"] == created["run_id"]
    if expected == "failed":
        assert final["error"] in {"safe failure", "cancelled"}
    manager.shutdown()
    store.close()
