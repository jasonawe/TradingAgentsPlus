"""HTTP API: /api/runs/{id}/artifacts and /api/runs/{id}/retry."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from web.app import create_app
from web.error_codes import TerminalReason
from web.manager import RunManager
from web.models import AnalysisRequest


def _request(**overrides):
    value = {
        "ticker": "AAPL",
        "analysis_date": "2026-08-26",
        "asset_type": "stock",
        "analysts": ["market"],
        "research_depth": 1,
    }
    value.update(overrides)
    return AnalysisRequest(**value)


def _build_app(tmp_path: Path):
    manager = RunManager()
    app = create_app(
        manager=manager,
        config={"results_dir": str(tmp_path), "project_dir": str(tmp_path)},
    )
    return app, manager, app.state.artifact_repository


def test_get_artifacts_returns_empty_list_for_just_started_run(tmp_path):
    app, manager, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        record = manager.start_run(_request(), run_id="r1")
        manager.begin_run(record.run_id)
        response = client.get(f"/api/runs/{record.run_id}/artifacts")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "r1"
    assert body["artifacts"] == []
    assert body["artifact_count"] == 0
    assert body["has_partial_results"] is False
    manager.shutdown()


def test_get_artifacts_returns_partial_after_provider_failure(tmp_path):
    app, manager, repo = _build_app(tmp_path)
    client_ctx = TestClient(app)
    with client_ctx as client:
        record = manager.start_run(_request(), run_id="r2")
        manager.begin_run(record.run_id)
        repo.upsert(
            record.run_id,
            artifact_key="market_report",
            artifact_type="analyst_report",
            phase="Analyst Team",
            agent="Market Analyst",
            title="Market",
            content_markdown="in progress...",
            status="partial",
            sequence=0,
        )
        manager.fail_run(
            record.run_id,
            error_code=TerminalReason.MODEL_TIMEOUT.value,
            error_message="model timed out",
            failed_phase="Research Team",
            retryable=True,
        )
        response = client.get(f"/api/runs/{record.run_id}/artifacts")
    assert response.status_code == 200
    body = response.json()
    assert body["artifact_count"] == 1
    assert body["completed_artifact_count"] == 0
    assert body["has_partial_results"] is True
    assert body["artifacts"][0]["artifact_key"] == "market_report"
    assert body["artifacts"][0]["status"] == "partial"
    manager.shutdown()


def test_get_artifacts_404_for_unknown_run(tmp_path):
    app, manager, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/runs/missing/artifacts")
    assert response.status_code == 404
    manager.shutdown()


def test_retry_endpoint_returns_409_when_not_retryable(tmp_path):
    app, manager, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        record = manager.start_run(_request(), run_id="r3")
        manager.begin_run(record.run_id)
        manager.fail_run(
            record.run_id,
            error_code=TerminalReason.MODEL_AUTH_ERROR.value,
            error_message="bad key",
            retryable=False,
        )
        response = client.post(f"/api/runs/{record.run_id}/retry")
    assert response.status_code == 409
    manager.shutdown()


def test_retry_endpoint_returns_409_when_no_checkpoint(tmp_path):
    app, manager, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        record = manager.start_run(_request(), run_id="r4")
        manager.begin_run(record.run_id)
        manager.fail_run(
            record.run_id,
            error_code=TerminalReason.MODEL_TIMEOUT.value,
            error_message="timed out",
            retryable=True,
        )
        # Force-clear the resume checkpoint so can_retry() rejects.
        manager._state(record.run_id).record.resume_checkpoint_id = None
        manager._persist_locked(manager._state(record.run_id).record)
        response = client.post(f"/api/runs/{record.run_id}/retry")
    assert response.status_code == 409
    manager.shutdown()


def test_get_run_includes_recovery_fields(tmp_path):
    app, manager, _ = _build_app(tmp_path)
    with TestClient(app) as client:
        record = manager.start_run(_request(), run_id="r5")
        manager.begin_run(record.run_id)
        manager.fail_run(
            record.run_id,
            error_code=TerminalReason.MODEL_TIMEOUT.value,
            error_message="model timed out",
            failed_phase="Research Team",
            failed_agent="Bear Researcher",
            failed_provider="openai",
            failed_model="gpt-5.5",
            retryable=True,
        )
        response = client.get(f"/api/runs/{record.run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["terminal_reason"] == TerminalReason.MODEL_TIMEOUT.value
    assert body["failed_phase"] == "Research Team"
    assert body["failed_agent"] == "Bear Researcher"
    assert body["failed_provider"] == "openai"
    assert body["failed_model"] == "gpt-5.5"
    assert body["retryable"] is True
    assert body["artifact_count"] == 0
    manager.shutdown()


def test_get_run_artifact_counts_visible_to_clients(tmp_path):
    app, manager, repo = _build_app(tmp_path)
    with TestClient(app) as client:
        record = manager.start_run(_request(), run_id="r6")
        manager.begin_run(record.run_id)
        repo.upsert(
            record.run_id,
            artifact_key="market_report",
            artifact_type="analyst_report",
            phase="Analyst Team",
            agent="Market Analyst",
            title="Market",
            content_markdown="done",
            status="completed",
            sequence=0,
        )
        response = client.get(f"/api/runs/{record.run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["artifact_count"] == 1
    assert body["completed_artifact_count"] == 1
    assert body["has_partial_results"] is False
    manager.shutdown()
