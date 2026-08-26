import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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


def test_only_one_active_run_and_atomic_begin():
    manager = RunManager()
    first = manager.start_run(request())
    assert first.status is RunStatus.QUEUED
    with pytest.raises(ActiveRunError):
        manager.start_run(request("MSFT"))

    started = manager.begin_run(first.run_id)
    assert started.status is RunStatus.RUNNING
    assert started.started_at is not None
    events = manager.read_events(first.run_id, 0).events
    assert [event.event for event in events] == [EventName.RUN_STARTED]


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
    assert [event.seq for event in batch.events[1:]] == [4, 5]


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
