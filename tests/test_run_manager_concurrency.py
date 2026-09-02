"""RunManager concurrency-cap unit tests (Plan 1 — Manager Concurrency)."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import Condition, Event

import pytest

from web.manager import (
    AssetBusyError,
    MaxConcurrentRunsError,
    RunManager,
    _ManagedRun,
)
from web.models import AnalysisRequest, RunRecord, RunStatus


def _stub_request(ticker: str = "AAPL") -> AnalysisRequest:
    from datetime import date
    return AnalysisRequest(
        ticker=ticker,
        analysis_date=date(2026, 9, 2),
        asset_type="stock",
        analysts=["market"],
        research_depth=1,
    )


def _managed_stub(ticker: str) -> _ManagedRun:
    record = RunRecord(
        run_id="run-existing",
        request=_stub_request(ticker),
        status=RunStatus.RUNNING,
        queued_at=datetime.now(timezone.utc),
    )
    return _ManagedRun(
        record=record,
        events=deque(maxlen=8),
        condition=Condition(),
        cancel_event=Event(),
    )


# -- Task 1.1: cap provider ----------------------------------------------------


def test_concurrent_runs_cap_reads_default_when_settings_missing():
    manager = RunManager(store=None)
    assert manager.concurrent_runs_cap() == 3


def test_concurrent_runs_cap_clamps_out_of_range():
    manager = RunManager(store=None)
    manager.configure_concurrency(
        {"scheduler.max_concurrent_runs": {"value": 999, "source": "configured"}}
    )
    assert manager.concurrent_runs_cap() == 10
    manager.configure_concurrency(
        {"scheduler.max_concurrent_runs": {"value": 0, "source": "configured"}}
    )
    assert manager.concurrent_runs_cap() == 1


# -- Task 1.2: set-based active tracking --------------------------------------


def test_active_run_ids_starts_empty():
    manager = RunManager(store=None)
    assert manager.active_run_ids() == set()
    assert manager.active_run_id is None


def test_active_run_id_returns_member_when_multiple():
    manager = RunManager(store=None)
    manager._active_run_ids = {"run-a", "run-b"}
    assert manager.active_run_id in {"run-a", "run-b"}


# -- Task 1.3: capacity + ticker guards ---------------------------------------


def test_start_run_rejects_when_at_cap(monkeypatch):
    manager = RunManager(store=None)
    manager._active_run_ids = {"run-x", "run-y", "run-z"}
    monkeypatch.setattr(manager, "concurrent_runs_cap", lambda: 3)
    with pytest.raises(MaxConcurrentRunsError):
        manager.start_run(_stub_request())


def test_start_run_rejects_duplicate_ticker(monkeypatch):
    manager = RunManager(store=None)
    manager._active_run_ids = {"run-existing"}
    manager._records["run-existing"] = _managed_stub("AAPL")
    monkeypatch.setattr(manager, "concurrent_runs_cap", lambda: 3)
    with pytest.raises(AssetBusyError):
        manager.start_run(_stub_request("AAPL"))


# -- Task 1.4: lifecycle hooks ------------------------------------------------


def test_start_run_inserts_into_active_set(monkeypatch):
    manager = RunManager(store=None)
    monkeypatch.setattr(manager, "concurrent_runs_cap", lambda: 3)
    record = manager.start_run(_stub_request("AAPL"), worker=lambda rid: None)
    assert record.run_id in manager.active_run_ids()


def test_finish_removes_from_active_set(monkeypatch):
    manager = RunManager(store=None)
    monkeypatch.setattr(manager, "concurrent_runs_cap", lambda: 3)
    record = manager.start_run(_stub_request("AAPL"), worker=lambda rid: None)
    manager.complete_run(record.run_id, signal="buy", report_id="r-1")
    assert manager.active_run_ids() == set()


# -- Task 1.5: executor resize + multi-run admission --------------------------


def test_executor_grows_to_match_cap(monkeypatch):
    manager = RunManager(store=None)
    monkeypatch.setattr(manager, "concurrent_runs_cap", lambda: 5)
    manager._resize_executor()
    assert manager._executor._max_workers == 5  # type: ignore[attr-defined]


def test_three_runs_in_flight_simultaneously():
    manager = RunManager(store=None)
    manager.configure_concurrency(
        {"scheduler.max_concurrent_runs": {"value": 3, "source": "configured"}}
    )
    records = [
        manager.start_run(_stub_request(f"T{i}"), worker=lambda rid: None)
        for i in range(3)
    ]
    assert len(manager.active_run_ids()) == 3
    for record in records:
        manager.complete_run(record.run_id, signal="buy", report_id=f"r-{record.run_id}")
    assert manager.active_run_ids() == set()
