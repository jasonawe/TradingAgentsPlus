import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from web.manager import ActiveRunError, EventBatch, RunManager
from web.models import AnalysisRequest, EventName, RunStatus


def request(ticker="AAPL"):
    return AnalysisRequest(
        ticker=ticker,
        analysis_date="2026-08-26",
        asset_type="stock",
        analysts=["market"],
        research_depth=1,
    )


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def lifecycle_config(*, interval=5, lease=30, wall=300):
    return {
        "run_timeout_seconds": wall,
        "run_heartbeat_interval_seconds": interval,
        "run_heartbeat_timeout_seconds": lease,
    }


def write_report_gate(path: Path, *, status="completed") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "complete_report.md").write_text("# report\n", encoding="utf-8")
    (path / "run.json").write_text(
        json.dumps({"status": status, "report_id": path.name}),
        encoding="utf-8",
    )
    (path / "COMMITTED").write_text("ok\n", encoding="utf-8")


def test_only_one_active_run_and_atomic_begin():
    manager = RunManager()
    # Plan 1: cap defaults to 3; force cap=1 to exercise the legacy single-run invariant.
    manager.configure_concurrency(
        {"scheduler.max_concurrent_runs": {"value": 1, "source": "configured"}}
    )
    first = manager.start_run(request())
    assert first.status is RunStatus.QUEUED
    with pytest.raises(ActiveRunError):
        manager.start_run(request("MSFT"))

    started = manager.begin_run(first.run_id)
    assert started.status is RunStatus.RUNNING
    assert started.started_at is not None
    events = manager.read_events(first.run_id, 0).events
    assert [event.event for event in events] == [EventName.RUN_STARTED]


def test_sqlite_persists_terminal_run_metadata_across_manager_instances(tmp_path):
    database = tmp_path / "web-runs.sqlite3"
    manager = RunManager(db_path=database)
    run = manager.start_run(request(), run_id="run-persisted")
    manager.begin_run(run.run_id)
    manager.complete_run(run.run_id, signal="BUY", report_id="report-persisted")
    manager.shutdown()

    restored = RunManager(db_path=database)
    record = restored.get_run("run-persisted")
    assert record.status is RunStatus.COMPLETED
    assert record.request.ticker == "AAPL"
    assert record.report_id == "report-persisted"
    assert restored.active_run_id is None
    restored.shutdown()


def test_terminal_reason_is_canonical_for_failed_and_restarted_runs(tmp_path):
    from web.error_codes import TerminalReason

    database = tmp_path / "terminal-reason.sqlite3"
    manager = RunManager(db_path=database)
    failed = manager.start_run(request(), run_id="failed-run")
    manager.begin_run(failed.run_id)
    failed = manager.fail_run(
        failed.run_id,
        error_code="model_timeout",
        error_message="model timed out",
        failed_phase="Research Team",
        failed_agent="Bear Researcher",
        failed_provider="openai",
        failed_model="gpt-5.5",
    )
    assert failed.terminal_reason == failed.error_code == "model_timeout"
    assert failed.failed_phase == "Research Team"
    assert failed.failed_agent == "Bear Researcher"
    assert failed.failed_provider == "openai"
    assert failed.failed_model == "gpt-5.5"
    assert failed.retryable is True

    manager.start_run(request("MSFT"), run_id="restart-run")
    manager.shutdown()

    restored = RunManager(db_path=database)
    interrupted = restored.get_run("restart-run")
    assert (
        interrupted.terminal_reason
        == interrupted.error_code
        == TerminalReason.SERVICE_RESTARTED.value
    )
    assert interrupted.retryable is True
    restored.shutdown()


def test_persisted_timed_out_run_rejects_late_events_and_transitions(tmp_path):
    from web.error_codes import TerminalReason

    database = tmp_path / "timed-out-terminal.sqlite3"
    manager = RunManager(db_path=database)
    run = manager.start_run(request(), run_id="timed-out-run")
    manager.begin_run(run.run_id)
    with manager._store.connection() as conn:
        conn.execute(
            "UPDATE web_runs SET status='timed_out',terminal_reason=?,"
            "error_code=? WHERE run_id=?",
            (
                TerminalReason.RUN_DEADLINE_EXCEEDED.value,
                TerminalReason.RUN_DEADLINE_EXCEEDED.value,
                run.run_id,
            ),
        )
    manager.shutdown()

    restored = RunManager(db_path=database)
    before = restored.get_run(run.run_id)
    assert before.status is RunStatus.TIMED_OUT
    assert restored.read_events(run.run_id, 0).terminal is True
    assert restored.publish(
        run.run_id,
        EventName.MESSAGE,
        {"message_type": "late", "text": "ignored"},
    ) is None
    assert restored.complete_run(run.run_id, signal="BUY", report_id="late") == before
    assert restored.fail_run(
        run.run_id,
        error_code="late_failure",
        error_message="ignored",
    ) == before
    assert restored.cancel_run(run.run_id) == before
    restored.request_cancel(run.run_id)
    assert restored.is_cancelled(run.run_id) is False
    restored.shutdown()


def test_sqlite_persists_live_progress_snapshot_across_manager_instances(tmp_path):
    database = tmp_path / "progress.sqlite3"
    manager = RunManager(db_path=database)
    run = manager.start_run(request(), run_id="run-live-progress")
    manager.begin_run(run.run_id)
    manager.publish(
        run.run_id,
        EventName.PROGRESS,
        {"progress": 0.1, "phase": "Analyst Team", "current_agent": "Market Analyst"},
    )
    manager.shutdown()

    restored = RunManager(db_path=database)
    record = restored.get_run(run.run_id)
    assert record.phase == "Analyst Team"
    assert record.current_agent is None
    assert record.progress == 0.1
    restored.shutdown()


def test_terminal_events_are_durable_and_replayed_after_restart(tmp_path):
    database = tmp_path / "events.sqlite3"
    manager = RunManager(db_path=database)
    run = manager.start_run(request(), run_id="run-events")
    manager.begin_run(run.run_id)
    manager.complete_run(run.run_id, signal="BUY", report_id="report")
    manager.shutdown()

    restored = RunManager(db_path=database)
    events = restored.read_events(run.run_id, 0).events
    assert [event.event for event in events][-1] is EventName.RUN_COMPLETED
    restored.shutdown()


def test_direct_db_path_uses_full_store_migration_and_replays_restart_interrupt(tmp_path):
    database = tmp_path / "direct.sqlite3"
    manager = RunManager(db_path=database)
    assert manager._store is not None
    manager.start_run(request(), run_id="queued-restart")
    manager.shutdown()
    restored = RunManager(db_path=database)
    with restored._store.connection() as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert conn.execute("SELECT id FROM watchlists WHERE id='default'").fetchone() is not None
        assert conn.execute("SELECT COUNT(*) FROM web_run_events WHERE run_id='queued-restart'").fetchone()[0] >= 1
    events = restored.read_events("queued-restart", 0).events
    assert sum(event.event is EventName.RUN_INTERRUPTED for event in events) == 1
    restored.shutdown()


def test_events_have_monotonic_sequences_and_iso_timestamps():
    manager = RunManager()
    run = manager.start_run(request())
    manager.begin_run(run.run_id)
    manager.publish(run.run_id, EventName.MESSAGE, {"message_type": "log", "text": "hello"})
    manager.publish(run.run_id, EventName.ACTIVITY, {"activity_type": "graph_update", "name": "node", "summary": ""})
    events = manager.read_events(run.run_id, 0).events
    assert [event.seq for event in events] == [1, 2, 3]
    for event in events:
        assert datetime.fromisoformat(event.timestamp.isoformat())


def test_progress_events_update_the_persisted_run_snapshot():
    manager = RunManager()
    run = manager.start_run(request(), run_id="run-progress-snapshot")
    manager.begin_run(run.run_id)
    manager.publish(
        run.run_id,
        EventName.PHASE_CHANGED,
        {"phase": "Analyst Team", "phase_index": 1, "phase_count": 5, "status": "in_progress"},
    )
    manager.publish(
        run.run_id,
        EventName.AGENT_STATUS,
        {"agent": "Market Analyst", "status": "in_progress"},
    )
    manager.publish(
        run.run_id,
        EventName.PROGRESS,
        {"progress": 0.1, "phase": "Analyst Team", "current_agent": "Market Analyst"},
    )
    snapshot = manager.get_run(run.run_id)
    assert snapshot.phase == "Analyst Team"
    assert snapshot.current_agent == "Market Analyst"
    assert snapshot.progress == 0.1


def test_activity_heartbeat_is_throttled_and_only_updates_queued_or_running():
    """``heartbeat()`` is the activity heartbeat, not the worker lease.

    It updates ``last_activity_at`` and must not refresh ``worker_heartbeat_at``
    on its own; that field is the lease thread's responsibility.
    """

    from web.error_codes import TerminalReason

    clock = MutableClock(datetime(2026, 8, 31, tzinfo=timezone.utc))
    manager = RunManager(clock=clock, lifecycle_config=lifecycle_config())
    run = manager.start_run(request(), run_id="heartbeat")

    clock.advance(4)
    assert manager.heartbeat(run.run_id).last_activity_at == run.last_activity_at
    clock.advance(1)
    assert manager.heartbeat(run.run_id).last_activity_at == clock.value
    # ``worker_heartbeat_at`` keeps the original queued timestamp because the
    # activity heartbeat is throttled and never renews the lease.
    manager.begin_run(run.run_id)
    clock.advance(5)
    assert manager.heartbeat(run.run_id).last_activity_at == clock.value

    manager.cancel_run(run.run_id)
    clock.advance(5)
    terminal = manager.heartbeat(run.run_id)
    assert terminal.status is RunStatus.CANCELLED
    assert terminal.last_activity_at == clock.value - timedelta(seconds=5)
    manager.shutdown()


def test_worker_lease_renewal_only_accepts_the_issuing_token():
    """A stale Worker cannot resurrect a task by writing the old timestamp."""

    from web.lease import generate_owner_token

    clock = MutableClock(datetime(2026, 8, 31, tzinfo=timezone.utc))
    manager = RunManager(clock=clock, lifecycle_config=lifecycle_config())
    run = manager.start_run(request(), run_id="lease")
    manager.begin_run(run.run_id)
    # No active lease thread → manager holds no token yet.
    assert manager.renew_worker_lease(run.run_id, "anything") is False
    # Even with the right token shape, without a live lease we should reject.
    assert manager.renew_worker_lease(run.run_id, generate_owner_token()) is False
    manager.shutdown()


def test_progress_is_clamped_monotonic_and_late_lower_update_is_not_published():
    manager = RunManager()
    run = manager.start_run(request(), run_id="monotonic-progress")
    manager.begin_run(run.run_id)

    upper = manager.publish(
        run.run_id,
        EventName.PROGRESS,
        {"progress": 2.0, "phase": "Research", "current_agent": "Bull"},
    )
    assert upper is not None and upper.payload.progress == 1.0
    event_count = len(manager.read_events(run.run_id).events)
    assert manager.publish(
        run.run_id,
        EventName.PROGRESS,
        {"progress": 0.4, "phase": "Old", "current_agent": "Late"},
    ) is None
    snapshot = manager.get_run(run.run_id)
    assert snapshot.progress == 1.0
    assert snapshot.phase == "Research"
    assert snapshot.current_agent == "Bull"
    assert len(manager.read_events(run.run_id).events) == event_count
    manager.shutdown()


@pytest.mark.parametrize(
    ("expire_by", "expected_status", "expected_reason", "expected_message"),
    [
        ("wall", RunStatus.TIMED_OUT, "run_deadline_exceeded", "分析超过最长运行时间"),
        ("lease", RunStatus.INTERRUPTED, "worker_lease_expired", "分析执行器失去连接"),
    ],
)
def test_expiry_distinguishes_wall_deadline_from_lease_heartbeat(
    expire_by, expected_status, expected_reason, expected_message
):
    """Two distinct terminal paths: deadline vs. worker-lease expiry.

    Before the recovery design these were conflated as ``heartbeat_timeout``.
    The new design treats the fixed deadline as ``timed_out`` and the
    Worker-lease expiry as ``interrupted``.
    """

    clock = MutableClock(datetime(2026, 8, 31, tzinfo=timezone.utc))
    manager = RunManager(clock=clock, lifecycle_config=lifecycle_config())
    run = manager.start_run(request(), run_id=f"timeout-{expire_by}")
    manager.begin_run(run.run_id)
    manager.publish(
        run.run_id,
        EventName.PROGRESS,
        {"progress": 0.45, "phase": "Research", "current_agent": "Bull"},
    )
    if expire_by == "wall":
        manager._records[run.run_id].record.timeout_at = clock.value + timedelta(seconds=1)
        clock.advance(2)
    else:
        clock.advance(31)

    expired = manager.heartbeat(run.run_id)
    assert expired.status is expected_status
    assert expired.progress == 0.45
    assert expired.current_agent is None
    assert expired.terminal_reason == expired.error_code == expected_reason
    assert expected_message in (expired.error_message or "")
    assert manager.get_run(run.run_id) == expired
    assert manager.complete_run(run.run_id, signal="BUY", report_id="late") == expired
    events = manager.read_events(run.run_id).events
    terminal_event = (
        EventName.RUN_TIMED_OUT if expected_status is RunStatus.TIMED_OUT else EventName.RUN_INTERRUPTED
    )
    terminal = [event for event in events if event.event is terminal_event]
    assert len(terminal) == 1
    if expected_status is RunStatus.TIMED_OUT:
        assert terminal[0].payload.progress == 0.45
    manager.shutdown()


def test_publishing_is_excluded_from_wall_and_heartbeat_expiry(tmp_path):
    clock = MutableClock(datetime(2026, 8, 31, tzinfo=timezone.utc))
    manager = RunManager(
        clock=clock,
        lifecycle_config=lifecycle_config(),
        report_root=tmp_path / "web_reports",
    )
    run = manager.start_run(request(), run_id="publishing-exempt")
    manager.begin_run(run.run_id)
    manager.begin_publishing(run.run_id)
    clock.advance(1000)

    assert manager.check_expired(run.run_id).status is RunStatus.PUBLISHING
    assert manager.heartbeat(run.run_id).status is RunStatus.PUBLISHING
    assert not any(
        event.event is EventName.RUN_TIMED_OUT
        for event in manager.read_events(run.run_id).events
    )
    manager.shutdown()


@pytest.mark.parametrize(
    ("transition", "status", "reason", "progress"),
    [
        ("complete", RunStatus.COMPLETED, "completed", 1.0),
        ("fail", RunStatus.FAILED, "provider_error", 0.35),
        ("cancel", RunStatus.CANCELLED, "cancelled", 0.35),
    ],
)
def test_terminal_transition_normalizes_reason_agent_and_progress(
    transition, status, reason, progress
):
    manager = RunManager()
    run = manager.start_run(request(), run_id=f"terminal-{transition}")
    manager.begin_run(run.run_id)
    manager.publish(
        run.run_id,
        EventName.PROGRESS,
        {"progress": 0.35, "phase": "Research", "current_agent": "Bull"},
    )
    if transition == "complete":
        record = manager.complete_run(run.run_id, signal="BUY", report_id="report")
    elif transition == "fail":
        record = manager.fail_run(
            run.run_id, error_code="provider_error", error_message="failed"
        )
    else:
        record = manager.cancel_run(run.run_id)
    assert record.status is status
    assert record.terminal_reason == record.error_code == reason
    assert record.current_agent is None
    assert record.progress == progress
    manager.shutdown()


def test_complete_publishing_requires_the_committed_report_gate(tmp_path):
    report_root = tmp_path / "web_reports"
    manager = RunManager(report_root=report_root)
    run = manager.start_run(request(), run_id="incomplete-publication")
    manager.begin_run(run.run_id)
    manager.begin_publishing(run.run_id)
    report_dir = report_root / "AAPL" / "2026-08-26" / run.run_id
    report_dir.mkdir(parents=True)
    (report_dir / "complete_report.md").write_text("# incomplete", encoding="utf-8")

    failed = manager.complete_publishing(
        run.run_id, signal="BUY", report_id=run.run_id, report_dir=report_dir
    )
    assert failed.status is RunStatus.FAILED
    assert failed.terminal_reason == failed.error_code == "publish_incomplete"
    assert failed.current_agent is None
    assert manager.read_events(run.run_id).events[-1].event is EventName.RUN_FAILED
    manager.shutdown()


@pytest.mark.parametrize("scenario", ["valid", "invalid_final", "invalid_temporary"])
def test_restart_recovers_or_quarantines_publishing_runs(tmp_path, scenario):
    database = tmp_path / "recovery.sqlite3"
    report_root = tmp_path / "web_reports"
    manager = RunManager(db_path=database, report_root=report_root)
    run = manager.start_run(request(), run_id=f"publish-{scenario}")
    manager.begin_run(run.run_id)
    manager.begin_publishing(run.run_id)
    report_dir = report_root / "AAPL" / "2026-08-26" / run.run_id
    if scenario == "valid":
        write_report_gate(report_dir)
    elif scenario == "invalid_temporary":
        report_dir = report_dir.parent / ".tmp" / run.run_id
        report_dir.mkdir(parents=True)
        (report_dir / "partial.txt").write_text("partial", encoding="utf-8")
    else:
        report_dir.mkdir(parents=True)
        (report_dir / "partial.txt").write_text("partial", encoding="utf-8")
    manager.shutdown()

    restored = RunManager(db_path=database, report_root=report_root)
    record = restored.get_run(run.run_id)
    if scenario == "valid":
        assert record.status is RunStatus.COMPLETED
        assert record.progress == 1.0
        assert record.report_id == run.run_id
        assert report_dir.is_dir()
    else:
        assert record.status is RunStatus.FAILED
        assert record.terminal_reason == "publish_incomplete"
        assert not report_dir.exists()
        assert any((report_root / ".orphaned").iterdir())
    restored.shutdown()


def test_restart_repairs_a_missing_terminal_event_atomically(tmp_path):
    database = tmp_path / "repair.sqlite3"
    manager = RunManager(db_path=database)
    run = manager.start_run(request(), run_id="repair-terminal")
    manager.begin_run(run.run_id)
    manager.fail_run(run.run_id, error_code="provider_error", error_message="failed")
    with manager._store.connection() as conn:
        conn.execute(
            "DELETE FROM web_run_events WHERE run_id=? AND event='run_failed'",
            (run.run_id,),
        )
    manager.shutdown()

    restored = RunManager(db_path=database)
    events = restored.read_events(run.run_id).events
    assert sum(event.event is EventName.RUN_FAILED for event in events) == 1
    with restored._store.connection() as conn:
        row = conn.execute(
            "SELECT status,terminal_reason,error_code FROM web_runs WHERE run_id=?",
            (run.run_id,),
        ).fetchone()
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM web_run_events WHERE run_id=? AND event='run_failed'",
            (run.run_id,),
        ).fetchone()[0]
    assert tuple(row) == ("failed", "provider_error", "provider_error")
    assert terminal_count == 1
    restored.shutdown()


def test_stale_snapshot_uses_latest_real_cut_and_replays_only_later_events():
    manager = RunManager(event_limit=2)
    run = manager.start_run(request(), run_id="snapshot-cut")
    manager.begin_run(run.run_id)
    for text in ("one", "two", "three"):
        manager.publish(
            run.run_id,
            EventName.MESSAGE,
            {"message_type": "log", "text": text},
        )
    batch = manager.read_events(run.run_id, 0)
    snapshot = batch.events[0]
    assert snapshot.event is EventName.RUN_SNAPSHOT
    assert snapshot.payload.snapshot_seq == 4
    assert snapshot.payload.replay_from_seq == 5
    assert batch.events == [snapshot]
    manager.publish(
        run.run_id,
        EventName.MESSAGE,
        {"message_type": "log", "text": "later"},
    )
    assert [event.seq for event in manager.read_events(run.run_id, 4).events] == [5]
    manager.shutdown()


def test_shutdown_stops_the_daemon_watchdog_thread():
    manager = RunManager(watchdog_interval=0.01)
    thread = manager._watchdog_thread
    assert thread is not None and thread.daemon and thread.is_alive()
    manager.shutdown()
    assert not thread.is_alive()


def test_subscriber_cursors_are_independent_and_replay_is_ordered():
    manager = RunManager(event_limit=4)
    run = manager.start_run(request())
    manager.begin_run(run.run_id)
    for text in ("one", "two", "three"):
        manager.publish(run.run_id, EventName.MESSAGE, {"message_type": "log", "text": text})
    assert [e.seq for e in manager.read_events(run.run_id, 0).events] == [1, 2, 3, 4]
    assert [e.seq for e in manager.read_events(run.run_id, 2).events] == [3, 4]
    assert [e.seq for e in manager.read_events(run.run_id, 0).events] == [1, 2, 3, 4]


def test_stale_cursor_returns_snapshot_before_retained_events():
    manager = RunManager(event_limit=2)
    run = manager.start_run(request())
    manager.begin_run(run.run_id)
    for text in ("one", "two", "three"):
        manager.publish(run.run_id, EventName.MESSAGE, {"message_type": "log", "text": text})
    batch = manager.read_events(run.run_id, 0)
    assert batch.stale is True
    assert batch.events[0].event is EventName.RUN_SNAPSHOT
    assert batch.events[0].payload.run.run_id == run.run_id
    assert batch.events[0].payload.snapshot_seq == 4
    assert batch.events[1:] == []


def test_terminal_records_expire_and_terminal_cancel_is_idempotent():
    now = datetime.now(timezone.utc)
    manager = RunManager(terminal_ttl=timedelta(seconds=10), clock=lambda: now)
    run = manager.start_run(request())
    manager.begin_run(run.run_id)
    completed = manager.complete_run(run.run_id, signal="BUY", report_id="report-1")
    assert completed.status is RunStatus.COMPLETED
    assert manager.cancel_run(run.run_id).status is RunStatus.COMPLETED
    manager._records[run.run_id].terminal_expires_at = now - timedelta(seconds=1)
    manager.cleanup(now=now)
    with pytest.raises(KeyError):
        manager.get_run(run.run_id)


def test_cancel_active_run_sets_flag_and_terminal_transition_is_idempotent():
    manager = RunManager()
    run = manager.start_run(request())
    manager.begin_run(run.run_id)
    result = manager.request_cancel(run.run_id)
    assert result.status is RunStatus.RUNNING
    assert manager.is_cancelled(run.run_id)
    cancelled = manager.cancel_run(run.run_id, phase="research", current_agent="news")
    assert cancelled.status is RunStatus.CANCELLED
    assert manager.cancel_run(run.run_id).status is RunStatus.CANCELLED
    assert manager.active_run_id is None


def test_wait_for_events_times_out_without_losing_cursor():
    manager = RunManager()
    run = manager.start_run(request())
    manager.begin_run(run.run_id)
    batch = manager.wait_for_events(run.run_id, cursor=1, timeout=0.01)
    assert isinstance(batch, EventBatch)
    assert batch.events == []
    assert batch.timed_out is True


def test_cleanup_is_safe_when_called_concurrently_with_run_lifecycle():
    now = datetime.now(timezone.utc)
    manager = RunManager(terminal_ttl=timedelta(seconds=10), clock=lambda: now)
    for index in range(40):
        run = manager.start_run(request(f"T{index}"), run_id=f"run-{index}")
        manager.begin_run(run.run_id)
        manager.complete_run(run.run_id, signal="BUY", report_id=run.run_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: manager.cleanup(now=now + timedelta(seconds=11)), range(100)))

    assert manager.active_run_id is None


def test_non_terminal_events_are_dropped_after_terminal_transition():
    manager = RunManager()
    run = manager.start_run(request())
    manager.begin_run(run.run_id)
    manager.complete_run(run.run_id, signal="BUY", report_id="report-1")

    assert manager.publish(run.run_id, EventName.MESSAGE, {"message_type": "log", "text": "late"}) is None
    assert [event.event for event in manager.read_events(run.run_id).events] == [
        EventName.RUN_STARTED,
        EventName.RUN_COMPLETED,
    ]


def test_public_publish_cannot_append_duplicate_terminal_event_after_completion():
    manager = RunManager()
    run = manager.start_run(request())
    manager.begin_run(run.run_id)
    manager.complete_run(run.run_id, signal="BUY", report_id="report-1")
    event = manager.publish(
        run.run_id,
        EventName.RUN_COMPLETED,
        {"status": "completed", "signal": "BUY", "report_id": "report-1"},
    )

    assert event is None
    assert [event.event for event in manager.read_events(run.run_id).events] == [
        EventName.RUN_STARTED,
        EventName.RUN_COMPLETED,
    ]


def test_worker_exception_is_exposed_as_generic_error_without_raw_details():
    secret = "sk-test-secret"

    def worker(_run_id: str) -> None:
        raise RuntimeError(f"provider payload api_key={secret} body={{'token': '{secret}'}}")

    manager = RunManager()
    run = manager.start_run(request(), worker=worker)
    deadline = time.monotonic() + 2
    while manager.get_run(run.run_id).status is not RunStatus.FAILED and time.monotonic() < deadline:
        time.sleep(0.01)

    failed = manager.get_run(run.run_id)
    assert failed.status is RunStatus.FAILED
    assert failed.error_message == "analysis worker failed"
    assert secret not in failed.error_message
    terminal_events = manager.read_events(run.run_id).events
    assert terminal_events[-1].payload.error_message == "analysis worker failed"
    assert secret not in terminal_events[-1].model_dump_json()
