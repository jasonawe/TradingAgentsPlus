import json
import threading

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from web.app import create_app  # noqa: E402
from web.history import ReportHistory  # noqa: E402
from web.manager import RunManager  # noqa: E402


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
    assert "OPENAI_API_KEY" not in json.dumps(payload)
    assert "must-not-leak" not in json.dumps(payload)


@pytest.mark.parametrize("path", ["/", "/analysis", "/active", "/reports", "/settings", "/reports/run-1"])
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
        assert active.json()["run"]["run_id"] == run_id
        assert active.json()["run"]["status"] == "running"
        client.post(f"/api/runs/{run_id}/cancel")
        runner.release.set()
        assert client.get("/api/runs/active").json() == {"run": None}
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
        json.dumps({"report_id": "run-1", "ticker": "NVDA", "generated_at": "2026-08-26T10:00:00+00:00", "status": "completed"}),
        encoding="utf-8",
    )
    (report_dir / "COMMITTED").write_text("ok\n", encoding="utf-8")
    with TestClient(app) as client:
        listing = client.get("/api/history")
        assert listing.status_code == 200
        assert listing.json()[0]["report_id"] == "run-1"
        detail = client.get("/api/history/run-1")
        assert detail.status_code == 200
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


def _sse_data(text):
    for block in text.split("\n\n"):
        line = next((line for line in block.splitlines() if line.startswith("data: ")), None)
        if line:
            yield json.loads(line[6:])
