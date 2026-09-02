import json
import threading
import warnings
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.exceptions import StarletteDeprecationWarning  # noqa: E402

from web.app import create_app  # noqa: E402
from web.history import ReportHistory  # noqa: E402
from web.manager import RunManager  # noqa: E402
from web.models import AnalysisRequest, EventName  # noqa: E402
from web.repositories import SettingsRepository  # noqa: E402
from web.storage import SQLiteStore  # noqa: E402


def _request(**overrides):
    value = {
        "ticker": "NVDA",
        "analysis_date": "2026-08-26",
        "asset_type": "stock",
        "analysts": ["market", "news"],
        "research_depth": 1,
        "output_language": "English",
    }
    value.update(overrides)
    return value


class BlockingRunner:
    def __init__(self, manager):
        self.manager = manager
        self.started = threading.Event()
        self.release = threading.Event()

    def worker(self, run_id):
        self.started.set()
        self.release.wait(2)
        if not self.manager.is_cancelled(run_id):
            self.manager.complete_run(run_id, signal="BUY", report_id=run_id)
        else:
            self.manager.cancel_run(run_id, phase="Analyst Team")


@pytest.fixture
def harness(tmp_path):
    manager = RunManager()
    # Plan 1: enforce cap=1 so legacy single-run conflict tests still hold.
    manager.configure_concurrency(
        {"scheduler.max_concurrent_runs": {"value": 1, "source": "configured"}}
    )
    runner = BlockingRunner(manager)
    config = {
        "results_dir": str(tmp_path / "results"),
        "project_dir": str(tmp_path),
        "output_language": "English",
        "llm_provider": "openai",
        "deep_think_llm": "gpt-test",
        "OPENAI_API_KEY": "must-not-leak",
    }
    history = ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path)
    app = create_app(manager=manager, config=config, runner=runner, history=history)
    return app, manager, runner, tmp_path


def test_index_and_config_are_public_and_redacted(harness):
    app, _manager, _runner, _tmp = harness
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        payload = client.get("/api/config").json()
    assert payload["supported_asset_types"] == ["stock", "crypto"]
    assert payload["research_depths"] == [1, 3, 5]
    assert payload["provider"] == "openai"
    assert payload["configured"] == {
        "provider": "openai",
        "quick_model": "gpt-5.4-mini",
        "deep_model": "gpt-5.5",
        "output_language": "English",
    }
    assert len(payload["providers"]) >= 3
    assert len(payload["output_languages"]) == 11
    assert payload["analyst_options"][0]["label_key"] == "analysts.market"
    assert "OPENAI_API_KEY" not in json.dumps(payload)
    assert "must-not-leak" not in json.dumps(payload)


@pytest.mark.parametrize("path", ["/", "/analysis", "/active", "/reports", "/settings", "/reports/run-1", "/assets/688836.SS"])
def test_console_routes_serve_the_single_page_entrypoint(harness, path):
    app, _manager, _runner, _tmp = harness
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert 'id="setup-view"' in response.text
    assert 'id="report-view"' in response.text


def test_create_get_conflict_and_cancel(harness):
    app, manager, runner, _tmp = harness
    with TestClient(app) as client:
        response = client.post("/api/runs", json=_request())
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert response.json()["request"]["provider"] == "openai"
        assert response.json()["request"]["quick_model"] == "gpt-5.4-mini"
        assert response.json()["request"]["deep_model"] == "gpt-5.5"
        assert response.json()["request"]["analysts"] == ["market", "news"]
        assert runner.started.wait(1)
        assert client.post("/api/runs", json=_request(ticker="AAPL")).status_code == 409
        record = client.get(f"/api/runs/{run_id}")
        assert record.status_code == 200
        assert record.json()["status"] == "running"
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
        runner.release.set()
    manager.shutdown()


def test_active_run_endpoint_returns_the_current_running_analysis(harness):
    app, manager, runner, _tmp = harness
    with TestClient(app) as client:
        response = client.post("/api/runs", json=_request())
        run_id = response.json()["run_id"]
        assert runner.started.wait(1)
        active = client.get("/api/runs/active")
        assert active.status_code == 200
        runs = active.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["run_id"] == run_id
        assert runs[0]["status"] == "running"
        client.post(f"/api/runs/{run_id}/cancel")
        runner.release.set()
        assert client.get("/api/runs/active").json() == {"runs": []}
    manager.shutdown()


def test_validation_and_unknown_run_errors(harness):
    app, _manager, _runner, _tmp = harness
    with TestClient(app) as client:
        assert client.post("/api/runs", json=_request(ticker="../secret")).status_code == 422
        assert client.post("/api/runs", json=_request(analysts=[])).status_code == 422
        invalid = client.post("/api/runs", json=_request(provider="openai", quick_model="not-a-model"))
        assert invalid.status_code == 422
        assert invalid.json() == {"detail": "invalid analysis configuration"}
        assert client.get("/api/runs/no-such").status_code == 404
        assert client.get("/api/runs/no-such/events").status_code == 404
        assert client.post("/api/runs/no-such/cancel").status_code == 404


def test_validation_does_not_emit_starlette_status_deprecation(harness):
    app, _manager, _runner, _tmp = harness
    with warnings.catch_warnings():
        warnings.simplefilter("error", StarletteDeprecationWarning)
        with TestClient(app) as client:
            assert client.post("/api/runs", json=_request(ticker="../secret")).status_code == 422
            invalid = client.post(
                "/api/runs",
                json=_request(provider="openai", quick_model="not-a-model"),
            )
            assert invalid.status_code == 422
            assert invalid.json() == {"detail": "invalid analysis configuration"}


def test_create_app_injects_lifecycle_config_and_start_run_freezes_it(tmp_path):
    store = SQLiteStore(tmp_path / "runs.sqlite3")
    SettingsRepository(store).set("run_timeout_seconds", 3600)
    fixed_now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    manager = RunManager(store=store, clock=lambda: fixed_now)
    app = create_app(
        manager=manager,
        config={
            "results_dir": str(tmp_path),
            "project_dir": str(tmp_path),
            "run_timeout_seconds": 5400,
            "run_heartbeat_interval_seconds": 20,
            "run_heartbeat_timeout_seconds": 120,
        },
    )
    assert manager.lifecycle_config["run_timeout_seconds"] == {
        "value": 3600,
        "source": "sqlite",
    }
    request = AnalysisRequest(
        ticker="AAPL",
        analysis_date="2026-08-27",
        analysts=["market"],
        research_depth=1,
    )
    run = manager.start_run(request, run_id="lifecycle")
    assert run.last_heartbeat_at == fixed_now
    assert run.timeout_at == datetime(2026, 8, 27, 1, tzinfo=timezone.utc)
    assert (run.run_timeout_seconds, run.run_heartbeat_interval_seconds, run.run_heartbeat_timeout_seconds) == (3600, 20, 120)
    with store.connection() as conn:
        stored = conn.execute(
            "SELECT timeout_at,run_timeout_seconds,run_heartbeat_interval_seconds,"
            "run_heartbeat_timeout_seconds FROM web_runs WHERE run_id='lifecycle'"
        ).fetchone()
    assert tuple(stored) == (run.timeout_at.isoformat(), 3600, 20, 120)
    assert app.state.manager is manager
    manager.shutdown()
    store.close()


def test_create_app_rejects_invalid_lifecycle_configuration(tmp_path):
    manager = RunManager()
    try:
        with pytest.raises(ValueError, match="run_timeout_seconds"):
            create_app(
                manager=manager,
                config={
                    "results_dir": str(tmp_path),
                    "project_dir": str(tmp_path),
                    "run_timeout_seconds": 299,
                },
            )
    finally:
        manager.shutdown()


def test_sse_emits_envelopes_and_last_event_id_wins(harness):
    app, manager, runner, _tmp = harness
    with TestClient(app) as client:
        response = client.post("/api/runs", json=_request())
        run_id = response.json()["run_id"]
        assert runner.started.wait(1)
        manager.publish(run_id, "message", {"message_type": "status", "text": "hello"})
        manager.complete_run(run_id, signal=None, report_id=run_id)
        stream = client.get(
            f"/api/runs/{run_id}/events?after_seq=0",
            headers={"Last-Event-ID": "1"},
        )
        assert stream.status_code == 200
        assert "event: run_completed" in stream.text
        assert "event: run_started" not in stream.text
        ids = [int(line[4:]) for line in stream.text.splitlines() if line.startswith("id: ")]
        assert ids == sorted(ids)
        data = list(_sse_data(stream.text))
        assert all("run_id" in item and isinstance(item["seq"], int) and "timestamp" in item for item in data)
        message = next(item for item in data if item["event"] == "message")
        assert message["payload"]["text"] == "hello"
        runner.release.set()
    manager.shutdown()


def test_sse_snapshot_has_no_id_and_exposes_locked_replay_cut(tmp_path):
    manager = RunManager(event_limit=2)
    app = create_app(
        manager=manager,
        config={"results_dir": str(tmp_path), "project_dir": str(tmp_path)},
    )
    run = manager.start_run(AnalysisRequest(**_request()), run_id="snapshot-sse")
    manager.begin_run(run.run_id)
    for text in ("one", "two", "three"):
        manager.publish(
            run.run_id,
            EventName.MESSAGE,
            {"message_type": "status", "text": text},
        )
    manager.fail_run(run.run_id, error_code="test_failure", error_message="failed")

    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run.run_id}/events?after_seq=0")
    snapshot_block = next(
        block for block in response.text.split("\n\n") if "event: run_snapshot" in block
    )
    assert "id: " not in snapshot_block
    payload = next(_sse_data(snapshot_block))
    assert payload["payload"]["snapshot_seq"] == 5
    assert payload["payload"]["replay_from_seq"] == 6


def test_fastapi_lifespan_stops_manager_watchdog(tmp_path):
    manager = RunManager(watchdog_interval=0.01)
    thread = manager._watchdog_thread
    app = create_app(
        manager=manager,
        config={"results_dir": str(tmp_path), "project_dir": str(tmp_path)},
    )
    with TestClient(app) as client:
        assert client.get("/api/config").status_code == 200
        assert thread is not None and thread.is_alive()
    assert not thread.is_alive()


def test_history_detail_and_download_are_allowlisted(harness):
    app, _manager, _runner, tmp_path = harness
    report_dir = tmp_path / "results" / "web_reports" / "NVDA" / "2026-08-26" / "run-1"
    report_dir.mkdir(parents=True)
    (report_dir / "complete_report.md").write_text(
        "# report\n\n---\n\n| 指标 | 数值 |\n|---|---:|\n| RSI | **81.88** |\n\n<script>alert('xss')</script>\n",
        encoding="utf-8",
    )
    (report_dir / "executive_summary.md").write_text("## Executive summary\n\nHold", encoding="utf-8")
    (report_dir / "1_analysts").mkdir()
    (report_dir / "1_analysts" / "market.md").write_text(
        "## Market\n\n| 指标 | 数值 |\n|---|---:|\n| RSI | **81.88** |\n",
        encoding="utf-8",
    )
    (report_dir / "run.json").write_text(
        json.dumps({"report_id": "run-1", "ticker": "NVDA", "generated_at": "2026-08-26T10:00:00+00:00", "status": "completed", "signal": "Underweight"}),
        encoding="utf-8",
    )
    (report_dir / "COMMITTED").write_text("ok\n", encoding="utf-8")
    with TestClient(app) as client:
        listing = client.get("/api/history")
        assert listing.status_code == 200
        assert listing.json()[0]["report_id"] == "run-1"
        assert listing.json()[0]["signal"] == "Underweight"
        assert listing.json()[0]["rating"] == "Underweight"
        detail = client.get("/api/history/run-1")
        assert detail.status_code == 200
        assert detail.json()["signal"] == "Underweight"
        assert detail.json()["rating"] == "Underweight"
        assert detail.json()["complete_report"].startswith("# report\n")
        assert "<table>" in detail.json()["complete_report_html"]
        assert "<hr>" in detail.json()["complete_report_html"]
        assert "<script" not in detail.json()["complete_report_html"]
        assert "executive_summary_html" in detail.json()
        assert "<table>" in detail.json()["analysts_html"]["market"]
        download = client.get("/api/history/run-1/download")
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("text/markdown")
        assert "attachment" in download.headers["content-disposition"]
        assert download.text.startswith("## Executive summary")
        assert client.get("/api/history/../../etc/passwd").status_code in (404, 400)
        assert client.get("/api/history/unknown/download").status_code == 404


def test_history_pagination_filters_and_preserves_legacy_response(harness):
    app, _manager, _runner, tmp_path = harness
    root = tmp_path / "results" / "web_reports"
    for report_id, ticker, generated, status in (
        ("r1", "AAPL", "2026-08-28T10:00:00+00:00", "completed"),
        ("r2", "MSFT", "2026-08-27T10:00:00+00:00", "completed"),
        ("r3", "AAPL", "2026-08-26T10:00:00+00:00", "timed_out"),
    ):
        report_dir = root / ticker / "2026-08-27" / report_id
        report_dir.mkdir(parents=True)
        (report_dir / "complete_report.md").write_text(
            f"# Trading Analysis Report: {ticker}\n\n## V. Portfolio Manager Decision\n\n{ticker} growth",
            encoding="utf-8",
        )
        (report_dir / "run.json").write_text(
            json.dumps({"report_id": report_id, "ticker": ticker, "generated_at": generated, "analysis_date": "2026-08-27", "asset_type": "stock", "status": status}),
            encoding="utf-8",
        )
        (report_dir / "COMMITTED").write_text("ok\n", encoding="utf-8")

    with TestClient(app) as client:
        legacy = client.get("/api/history")
        assert isinstance(legacy.json(), list)
        page = client.get("/api/history?page=1&page_size=1&query=growth&asset_type=stock&status=completed&sort=generated_at_desc")
        assert page.status_code == 200
        assert page.json()["page"] == 1
        assert page.json()["page_size"] == 1
        assert page.json()["total"] == 2
        assert page.json()["has_next"] is True
        assert [item["report_id"] for item in page.json()["items"]] == ["r1"]
        exact = client.get("/api/history?page=1&ticker=msft&query=AAPL")
        assert [item["report_id"] for item in exact.json()["items"]] == ["r2"]
        empty = client.get("/api/history?page=99&page_size=20")
        assert empty.status_code == 200
        assert empty.json()["items"] == []
        assert empty.json()["has_next"] is False


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page_size=101",
        "status=running",
        "asset_type=bond",
        "sort=random",
        "date_from=2026-08-28&date_to=2026-08-27",
    ],
)
def test_history_pagination_rejects_invalid_parameters(harness, query):
    app, _manager, _runner, _tmp = harness
    with TestClient(app) as client:
        assert client.get(f"/api/history?{query}").status_code == 422


def test_market_provider_health_is_exposed_in_provider_and_settings_views(harness):
    app, _manager, _runner, _tmp = harness
    health = app.state.repositories["provider_health"]
    health.record_failure("yfinance", "timeout", "down", 25.0)
    with TestClient(app) as client:
        providers = client.get("/api/providers/market-data").json()["providers"]
        yfinance = next(item for item in providers if item["id"] == "yfinance")
        assert yfinance["status"] == "degraded"
        assert yfinance["status_key"] == "provider_status.degraded"
        assert yfinance["health"]["failure_count"] == 1
        assert yfinance["health"]["last_latency_ms"] == 25.0
        settings = client.get("/api/settings").json()
        assert settings["provider_health"]["yfinance"]["failure_count"] == 1


def _sse_data(text):
    for block in text.split("\n\n"):
        line = next((line for line in block.splitlines() if line.startswith("data: ")), None)
        if line:
            yield json.loads(line[6:])


def test_list_runs_for_ticker_filters_orders_and_caps(tmp_path):
    manager = RunManager()
    try:
        # Add a failed NVDA run directly into the manager's records
        # via a synthetic flow that doesn't trip the single-active policy.
        nvda_a = manager.start_run(AnalysisRequest(**_request(ticker="NVDA")), run_id="run-nvda-a")
        manager.begin_run(nvda_a.run_id)
        manager.fail_run(
            nvda_a.run_id,
            error_code="model_timeout",
            error_message="model timed out",
            failed_phase="Risk Management",
            failed_agent="Aggressive Analyst",
        )

        # Only NVDA so far — case-insensitive lookup.
        results = manager.list_runs_for_ticker("nvda")
        assert [r.run_id for r in results] == ["run-nvda-a"]
        assert results[0].request.ticker == "NVDA"
        assert results[0].failed_agent == "Aggressive Analyst"

        # Empty ticker yields no records.
        assert manager.list_runs_for_ticker("") == []

        # Limit caps the result list.
        assert len(manager.list_runs_for_ticker("NVDA", limit=0)) == 1  # floor of 1
    finally:
        manager.shutdown()


def test_list_runs_for_ticker_isolates_tickers(tmp_path):
    """Two managers keep their runs isolated."""
    manager_a = RunManager()
    manager_b = RunManager()
    try:
        record = manager_a.start_run(AnalysisRequest(**_request(ticker="AAPL")), run_id="run-aapl-only")
        manager_a.begin_run(record.run_id)
        manager_a.fail_run(record.run_id, error_code="provider_error", error_message="x")
        # B should see nothing for AAPL.
        assert manager_b.list_runs_for_ticker("AAPL") == []
        # A should see its own.
        assert [r.run_id for r in manager_a.list_runs_for_ticker("AAPL")] == ["run-aapl-only"]
    finally:
        manager_a.shutdown()
        manager_b.shutdown()


def test_asset_runs_endpoint_returns_only_matching_ticker(harness):
    app, manager, runner, _tmp = harness
    nvda = manager.start_run(AnalysisRequest(**_request(ticker="NVDA")), run_id="run-nvda-detail")
    manager.begin_run(nvda.run_id)
    manager.fail_run(
        nvda.run_id,
        error_code="model_timeout",
        error_message="request_timed_out",
        failed_phase="Risk Management",
        failed_agent="Aggressive Analyst",
    )
    aapl = manager.start_run(AnalysisRequest(**_request(ticker="AAPL")), run_id="run-aapl-detail")
    manager.begin_run(aapl.run_id)
    runner.release.set()
    try:
        with TestClient(app) as client:
            response = client.get("/api/assets/NVDA/runs?limit=10")
            assert response.status_code == 200
            body = response.json()
            assert body["symbol"] == "NVDA"
            run_ids = [item["run_id"] for item in body["items"]]
            assert "run-nvda-detail" in run_ids
            assert "run-aapl-detail" not in run_ids
            failed = next(item for item in body["items"] if item["run_id"] == "run-nvda-detail")
            assert failed["status"] == "failed"
            assert failed["failed_agent"] == "Aggressive Analyst"
            assert failed["retryable"] is True
            assert failed["provider"] is None  # _request helper omits provider
    finally:
        manager.shutdown()

def test_runs_active_returns_list(harness):
    """GET /api/runs/active must return {"runs": [...]} — never {"run": ...}."""
    app, _manager, _runner, _tmp = harness
    with TestClient(app) as client:
        response = client.get("/api/runs/active")
        assert response.status_code == 200
        body = response.json()
        assert "runs" in body
        assert isinstance(body["runs"], list)
        assert "run" not in body  # legacy single-run key removed


def test_runs_active_lists_each_in_flight_run(harness):
    """When two runs are active, both appear in the response."""
    app, manager, runner, _tmp = harness
    manager.configure_concurrency(
        {"scheduler.max_concurrent_runs": {"value": 3, "source": "configured"}}
    )
    from web.models import AnalysisRequest
    from datetime import date
    def _analysis_request(ticker):
        return AnalysisRequest(
            ticker=ticker, analysis_date=date(2026, 9, 2),
            asset_type="stock", analysts=["market"], research_depth=1,
        )
    with TestClient(app) as client:
        manager.start_run(_analysis_request("AAPL"), worker=lambda rid: None)
        manager.start_run(_analysis_request("MSFT"), worker=lambda rid: None)
        response = client.get("/api/runs/active")
        assert response.status_code == 200
        symbols = {item["request"]["ticker"] for item in response.json()["runs"]}
        assert symbols == {"AAPL", "MSFT"}

