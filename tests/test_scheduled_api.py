import time
from datetime import datetime, timezone
from unittest.mock import Mock

from fastapi.testclient import TestClient

from web.app import create_app
from web.error_codes import TerminalReason
from web.history import ReportHistory
from web.manager import RunManager
from web.repositories import SettingsRepository
from web.storage import SQLiteStore


class CompletingRunner:
    def __init__(self, manager):
        self.manager = manager

    def worker(self, run_id):
        self.manager.complete_run(run_id, signal="BUY", report_id=run_id)


def _app(tmp_path, *, max_concurrent_runs=3):
    store = SQLiteStore(tmp_path / "scheduled-api.sqlite3")
    settings = SettingsRepository(store)
    settings.update_scheduler_settings(max_concurrent_runs=max_concurrent_runs)
    manager = RunManager(store=store)
    config = {
        "results_dir": str(tmp_path / "results"),
        "project_dir": str(tmp_path),
        "output_language": "English",
        "llm_provider": "openai",
        "deep_think_llm": "gpt-5.5",
        "quick_think_llm": "gpt-5.4-mini",
    }
    app = create_app(
        manager=manager,
        config=config,
        runner=CompletingRunner(manager),
        history=ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path),
    )
    return app, manager, store


def _add_asset(client, symbol="AAPL", asset_type="stock"):
    response = client.post(
        "/api/watchlist/items", json={"symbol": symbol, "asset_type": asset_type}
    )
    assert response.status_code == 200


def _create_job(client, symbol="AAPL", asset_type="stock", cron="0 9 * * *"):
    return client.post(
        "/api/scheduled/jobs",
        json={
            "symbol": symbol,
            "asset_type": asset_type,
            "cron_expression": cron,
            "note": "morning",
        },
    )


def test_scheduled_console_route_and_lifespan_manage_scheduler(tmp_path):
    app, manager, store = _app(tmp_path)
    scheduler = app.state.scheduler
    scheduler.start = Mock(wraps=scheduler.start)
    scheduler.shutdown = Mock(wraps=scheduler.shutdown)

    with TestClient(app) as client:
        assert client.get("/scheduled").status_code == 200
        scheduler.start.assert_called_once_with()
    scheduler.shutdown.assert_called_once_with()
    assert manager._shutdown is True
    store.close()


def test_scheduled_job_crud_toggle_list_detail_and_logs(tmp_path):
    app, manager, store = _app(tmp_path)
    with TestClient(app) as client:
        _add_asset(client)
        created_response = _create_job(client)
        assert created_response.status_code == 201
        created = created_response.json()
        assert created["symbol"] == "AAPL"
        assert created["next_run_at"] is not None
        assert created["last_run_at"] is None
        assert created["last_run_status"] is None

        listing = client.get("/api/scheduled/jobs")
        assert listing.status_code == 200
        assert listing.json()["items"][0]["id"] == created["id"]
        assert len(listing.json()["version"]) == 16
        assert client.get(f"/api/scheduled/jobs/{created['id']}").json() == created

        updated_response = client.patch(
            f"/api/scheduled/jobs/{created['id']}",
            json={"cron_expression": "30 9 * * 1-5", "note": None},
        )
        assert updated_response.status_code == 201
        assert updated_response.json()["cron_expression"] == "30 9 * * 1-5"
        toggled = client.post(
            f"/api/scheduled/jobs/{created['id']}/toggle", json={"enabled": False}
        )
        assert toggled.status_code == 201
        assert toggled.json()["enabled"] is False
        assert toggled.json()["next_run_at"] is None

        run = client.post(f"/api/scheduled/jobs/{created['id']}/run")
        assert run.status_code == 202
        assert run.json()["run_id"] is not None
        logs = client.get(f"/api/scheduled/jobs/{created['id']}/logs?limit=5")
        assert logs.status_code == 200
        assert logs.json()["items"][0]["job_id"] == created["id"]

        deleted = client.delete(f"/api/scheduled/jobs/{created['id']}")
        assert deleted.status_code == 204
        assert client.get(f"/api/scheduled/jobs/{created['id']}").status_code == 404
    manager.shutdown()
    store.close()


def test_deleting_watchlist_asset_cascades_job_logs_and_registration(tmp_path):
    app, manager, store = _app(tmp_path)
    with TestClient(app) as client:
        _add_asset(client)
        job = _create_job(client).json()
        now = datetime.now(timezone.utc).isoformat()
        app.state.repositories["scheduled_logs"].create(
            job["id"],
            symbol="AAPL",
            asset_type="stock",
            scheduled_for=now,
            fired_at=now,
            status="skipped",
            skip_reason="capacity",
        )
        watchlist = client.get("/api/watchlist").json()
        item = watchlist["items"][0]

        response = client.delete(
            f"/api/watchlist/items/{item['id']}",
            params={"version": watchlist["watchlist"]["version"]},
        )

        assert response.status_code == 204
        assert client.get("/api/scheduled/jobs").json()["items"] == []
        assert app.state.repositories["scheduled_logs"].list(job["id"]) == []
        assert app.state.scheduler.scheduler.get_job(job["id"]) is None
    manager.shutdown()
    store.close()


def test_scheduled_job_guards_and_human_errors(tmp_path):
    app, manager, store = _app(tmp_path)
    with TestClient(app) as client:
        missing = _create_job(client)
        assert missing.status_code == 422
        assert missing.json() == {"detail": "asset must exist in the watchlist"}

        _add_asset(client)
        assert client.post(
            "/api/scheduled/jobs",
            json={
                "symbol": "AAPL",
                "asset_type": "stock",
                "cron_expression": "0 9 * * *",
                "enabled": False,
            },
        ).status_code == 422
        invalid = _create_job(client, cron="61 9 * * *")
        assert invalid.status_code == 422
        assert "cron" in invalid.json()["detail"].lower()
        created = _create_job(client)
        assert created.status_code == 201
        duplicate = _create_job(client)
        assert duplicate.status_code == 409
        assert duplicate.json() == {"detail": "scheduled job already exists for asset"}

        job_id = created.json()["id"]
        assert client.patch(f"/api/scheduled/jobs/{job_id}", json={}).status_code == 422
        assert client.patch(f"/api/scheduled/jobs/{job_id}", json={"wat": 1}).status_code == 422
        assert client.patch(
            f"/api/scheduled/jobs/{job_id}", json={"cron_expression": None}
        ).status_code == 422
        assert client.patch(
            f"/api/scheduled/jobs/{job_id}", json={"enabled": None}
        ).status_code == 422
        assert client.patch(
            f"/api/scheduled/jobs/{job_id}", json={"note": {"bad": "shape"}}
        ).status_code == 422
        assert client.post(f"/api/scheduled/jobs/{job_id}/toggle", json={"enabled": "no"}).status_code == 422
        assert client.get("/api/scheduled/jobs/missing").status_code == 404
        assert client.get("/api/scheduled/jobs/missing/logs").status_code == 404
        assert client.post("/api/scheduled/jobs/missing/run").status_code == 404
        assert client.delete("/api/scheduled/jobs/missing").status_code == 404
    manager.shutdown()
    store.close()


def test_cron_preview_and_scheduler_settings_reconfigure_manager(tmp_path):
    app, manager, store = _app(tmp_path, max_concurrent_runs=2)
    with TestClient(app) as client:
        settings = client.get("/api/scheduled/settings")
        assert settings.json() == {"enabled": True, "max_concurrent_runs": 2}
        assert manager.concurrent_runs_cap() == 2

        preview = client.get(
            "/api/scheduled/cron/preview",
            params={"cron_expression": "0 9 * * *", "count": 2},
        )
        assert preview.status_code == 200
        assert preview.json()["cron_expression"] == "0 9 * * *"
        assert len(preview.json()["next_run_times"]) == 2
        assert client.get(
            "/api/scheduled/cron/preview", params={"cron_expression": "bad"}
        ).status_code == 422

        updated = client.patch(
            "/api/scheduled/settings",
            json={"enabled": False, "max_concurrent_runs": 8},
        )
        assert updated.status_code == 200
        assert updated.json() == {"enabled": False, "max_concurrent_runs": 8}
        assert manager.concurrent_runs_cap() == 8
        assert client.patch(
            "/api/scheduled/settings", json={"max_concurrent_runs": 0}
        ).status_code == 422
        assert client.patch(
            "/api/scheduled/settings", json={"max_concurrent_runs": "8"}
        ).status_code == 422
        assert client.patch(
            "/api/scheduled/settings", json={"unknown": True}
        ).status_code == 422
    manager.shutdown()
    store.close()


def test_manual_run_now_bypasses_disabled_master_and_job(tmp_path):
    app, manager, store = _app(tmp_path)
    with TestClient(app) as client:
        _add_asset(client)
        job = _create_job(client).json()
        client.post(f"/api/scheduled/jobs/{job['id']}/toggle", json={"enabled": False})
        client.patch("/api/scheduled/settings", json={"enabled": False})

        response = client.post(f"/api/scheduled/jobs/{job['id']}/run")
        assert response.status_code == 202
        assert response.json()["skip_reason"] is None
        assert response.json()["run_id"]
    manager.shutdown()
    store.close()


def test_retry_endpoint_reuses_the_app_worker(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "retry-worker.sqlite3")
    manager = RunManager(store=store)
    app = create_app(
        manager=manager,
        config={
            "results_dir": str(tmp_path / "results"),
            "project_dir": str(tmp_path),
            "output_language": "English",
            "llm_provider": "openai",
        },
        history=ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path),
    )
    parent = manager.start_run(
        __import__("web.models", fromlist=["AnalysisRequest"]).AnalysisRequest(
            ticker="AAPL",
            analysis_date="2026-09-02",
            analysts=["market"],
            research_depth=1,
        ),
        run_id="retry-parent",
    )
    manager.begin_run(parent.run_id)
    manager.fail_run(
        parent.run_id,
        error_code=TerminalReason.MODEL_TIMEOUT.value,
        error_message="timed out",
        retryable=True,
    )
    manager._state(parent.run_id).record.resume_checkpoint_id = "checkpoint-1"
    manager._persist_locked(manager._state(parent.run_id).record)
    monkeypatch.setattr(manager, "can_retry", lambda _run_id: (True, "retryable"))
    seen = {}

    def fake_retry_run(*, parent_run_id, request, worker):
        seen["worker"] = worker
        return manager.get_run(parent_run_id)

    monkeypatch.setattr(manager, "retry_run", fake_retry_run)
    with TestClient(app) as client:
        response = client.post(f"/api/runs/{parent.run_id}/retry")

    assert response.status_code == 202
    assert seen["worker"] is app.state.worker
    manager.shutdown()
    store.close()


def test_run_now_returns_capacity_skip_when_manager_is_full(tmp_path):
    app, manager, store = _app(tmp_path, max_concurrent_runs=1)
    with TestClient(app) as client:
        _add_asset(client, "AAPL")
        _add_asset(client, "MSFT")
        job = _create_job(client, "MSFT").json()
        manager.start_run(
            __import__("web.models", fromlist=["AnalysisRequest"]).AnalysisRequest(
                ticker="AAPL",
                analysis_date="2026-09-02",
                analysts=["market"],
                research_depth=1,
            ),
            worker=lambda _run_id: time.sleep(1),
        )

        response = client.post(f"/api/scheduled/jobs/{job['id']}/run")

        assert response.status_code == 202
        assert response.json()["status"] == "skipped"
        assert response.json()["skip_reason"] == "capacity"
    manager.shutdown()
    store.close()
