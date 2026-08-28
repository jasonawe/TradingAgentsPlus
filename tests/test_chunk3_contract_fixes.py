import json
from fastapi.testclient import TestClient

from web.app import create_app
from web.history import ReportHistory
from web.manager import RunManager
from web.models import AnalysisRequest, EventName, RunStatus
from web.repositories import WatchlistRepository
from web.storage import SQLiteStore


def _request(**overrides):
    value = {
        "ticker": "AAPL",
        "analysis_date": "2026-08-27",
        "analysts": ["market"],
        "research_depth": 1,
    }
    value.update(overrides)
    return AnalysisRequest(**value)


def test_market_contracts_are_flattened_and_errors_are_chinese(tmp_path, monkeypatch):
    app = create_app(config={"results_dir": str(tmp_path), "output_language": "English"})
    quote = app.state.market_service
    monkeypatch.setattr(quote, "get_quotes", lambda symbols, asset_type: {
        "items": [{
            "symbol": "AAPL", "canonical_symbol": "AAPL", "asset_type": "stock",
            "price": 1.0, "quote_time": "2026-08-27T00:00:00Z",
            "fetched_at": "2026-08-27T00:00:01Z", "freshness": "fresh",
            "source": "test", "error": None,
        }], "partial": False,
    })
    with TestClient(app) as client:
        payload = client.get("/api/quotes?symbols=aapl").json()
        assert payload["items"][0]["canonical_symbol"] == "AAPL"
        settings = client.get("/api/settings").json()
        assert settings["fields"]["output_language"]["value"] == "English"
        assert all(item["status"] in {"ready", "not_configured", "error"} for item in client.get("/api/providers/market-data").json()["providers"])


def test_output_language_environment_overrides_sqlite(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    store_settings = __import__("web.repositories", fromlist=["SettingsRepository"]).SettingsRepository(store)
    store_settings.set("output_language", "Japanese")
    monkeypatch.setenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "Chinese")
    app = create_app(config={"results_dir": str(tmp_path), "output_language": "English"}, manager=RunManager(store=store))
    with TestClient(app) as client:
        assert client.get("/api/config").json()["effective_output_language"] == "Chinese"
        assert client.get("/api/settings").json()["fields"]["output_language"] == {"value": "Chinese", "source": "env"}


def test_run_effective_metadata_is_stored(tmp_path):
    manager = RunManager(db_path=tmp_path / "runs.sqlite3")
    run = manager.start_run(_request(quote_strategy_id="fallback-yfinance-alpha-vantage"), run_id="meta")
    stored = manager._store.connection()
    with stored as conn:
        record = conn.execute("SELECT effective_quote_strategy_id,effective_quote_provider_chain FROM web_runs WHERE run_id='meta'").fetchone()
        assert record[0] == "fallback-yfinance-alpha-vantage"
        assert json.loads(record[1]) == ["yfinance", "alpha_vantage"]
    manager.shutdown()


def test_restart_marks_run_interrupted_and_emits_only_interrupted(tmp_path):
    database = tmp_path / "runs.sqlite3"
    manager = RunManager(db_path=database)
    manager.start_run(_request(), run_id="queued")
    manager.shutdown()
    restored = RunManager(db_path=database)
    assert restored.get_run("queued").status is RunStatus.INTERRUPTED
    events = restored.read_events("queued").events
    assert events[-1].event is EventName.RUN_INTERRUPTED
    assert all(event.event is not EventName.RUN_FAILED for event in events)
    restored.shutdown()


def test_watchlist_patch_supports_position_with_cas(tmp_path):
    repo = WatchlistRepository(SQLiteStore(tmp_path / "db.sqlite3"))
    first = repo.add_item("AAPL", asset_type="stock")
    repo.add_item("MSFT", asset_type="stock")
    updated = repo.update_item(first["id"], expected_version=repo.get_default()["version"], position=1)
    assert updated["position"] == 1


def test_legacy_signal_maps_to_rating(tmp_path):
    root = tmp_path / "reports" / "AAPL_2026-08-27"
    root.mkdir(parents=True)
    (root / "complete_report.md").write_text("# Trading Analysis Report: AAPL", encoding="utf-8")
    (root / "run.json").write_text(json.dumps({"rating": "Hold"}), encoding="utf-8")
    record = ReportHistory(results_dir=tmp_path, cwd=tmp_path).list_reports()[0]
    assert record["rating"] == "Hold"
