import pytest

from web.repositories import (
    AnalysisRunRepository,
    QuoteRepository,
    ReportRepository,
    SettingsRepository,
    SnapshotRepository,
    WatchlistRepository,
)
from web.storage import SQLiteStore


def test_default_watchlist_is_idempotent_and_symbols_are_canonical(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = WatchlistRepository(store)
    first = repo.get_default()
    second = repo.get_default()
    assert first["id"] == second["id"] == "default"
    assert repo.add_item("aapl", asset_type="stock", note="Apple")["symbol"] == "AAPL"
    assert repo.add_item("msft", asset_type="stock")["symbol"] == "MSFT"
    with pytest.raises(ValueError, match="asset_type"):
        repo.add_item("GOOG", asset_type="bond")
    assert repo.list_items()[0]["symbol"] == "AAPL"
    try:
        repo.add_item(" AAPL ", asset_type="stock")
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate symbol should fail")
    store.close()


def test_watchlist_version_cas_and_quote_snapshot_repositories(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    watchlists = WatchlistRepository(store)
    item = watchlists.add_item("TSLA", asset_type="stock")
    assert watchlists.update_item(item["id"], expected_version=watchlists.get_default()["version"], note="EV")["note"] == "EV"
    quotes = QuoteRepository(store)
    quotes.upsert_quote({"symbol": "TSLA", "asset_type": "stock", "price": 200.0, "currency": "USD", "as_of": "2026-08-27T00:00:00+00:00", "freshness": "fresh", "source": "test"})
    assert quotes.get_latest("TSLA")["price"] == 200.0
    snapshots = SnapshotRepository(store)
    snapshots.save_manifest("run-1", {"manifest_hash": "abc", "datasets": {}})
    assert snapshots.get_manifest("run-1")["manifest_hash"] == "abc"
    with store.connection() as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_data_snapshots'").fetchone() is not None
    store.close()


def test_watchlist_mutations_derive_watchlist_and_use_cas(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = WatchlistRepository(store)
    first = repo.add_item("AAPL", asset_type="stock", watchlist_id="secondary")
    second = repo.add_item("MSFT", asset_type="stock", watchlist_id="secondary")
    version = repo.get("secondary")["version"]
    updated = repo.update_item(first["id"], expected_version=version, symbol="aapl", note="updated")
    assert updated["symbol"] == "AAPL"
    retained = repo.update_item(updated["id"], expected_version=repo.get("secondary")["version"])
    assert retained["note"] == "updated"
    with pytest.raises(ValueError, match="asset_type"):
        repo.update_item(second["id"], expected_version=repo.get("secondary")["version"], asset_type="bond")
    with pytest.raises(ValueError, match="symbol"):
        repo.update_item(second["id"], expected_version=repo.get("secondary")["version"], symbol="../bad")
    with pytest.raises(ValueError, match="duplicate"):
        repo.update_item(second["id"], expected_version=repo.get("secondary")["version"], symbol=" AAPL ")
    with pytest.raises(RuntimeError, match="version"):
        repo.delete_item(first["id"], expected_version=version)
    latest = repo.get("secondary")["version"]
    repo.delete_item(first["id"], expected_version=latest)
    assert [row["symbol"] for row in repo.list_items("secondary")] == ["MSFT"]
    store.close()


def test_quote_normalizes_symbol_and_rejects_unknown_freshness(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = QuoteRepository(store)
    repo.upsert_quote({"symbol": "aapl", "asset_type": "stock", "price": 1, "freshness": "fresh", "source": "test"})
    assert repo.get_latest("AAPL")["symbol"] == "AAPL"
    with pytest.raises(ValueError, match="freshness"):
        repo.upsert_quote({"symbol": "MSFT", "asset_type": "stock", "price": 1, "freshness": "unknown", "source": "test"})
    with pytest.raises(ValueError, match="asset_type"):
        repo.upsert_quote({"symbol": "MSFT", "asset_type": "bond", "price": 1, "freshness": "fresh", "source": "test"})
    with pytest.raises(ValueError, match="symbol"):
        repo.get_latest("../bad")
    with pytest.raises(ValueError, match="timestamp"):
        repo.upsert_candles([{"symbol": "aapl", "interval": "1d", "timestamp": "bad", "close": 1, "source": "test"}])
    repo.upsert_candles([{"symbol": "aapl", "interval": "1d", "timestamp": "2026-08-27T00:00:00+00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 2, "source": "test"}])
    assert repo.get_candles("AAPL", "1d")[0]["symbol"] == "AAPL"
    with pytest.raises(ValueError, match="interval"):
        repo.upsert_candles([{"symbol": "AAPL", "interval": "2d", "timestamp": "2026-08-27T00:00:00+00:00", "close": 1}])
    with pytest.raises(ValueError, match="asset_type"):
        repo.get_candles("AAPL", "1d", asset_type="bond")
    with pytest.raises(ValueError, match="interval"):
        repo.get_candles("AAPL", "2d")
    store.close()


def test_snapshot_manifest_is_insert_once_and_finalizable(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = SnapshotRepository(store)
    manifest = {"manifest_hash": "abc", "datasets": {"quote": "hash"}}
    repo.save_manifest("run-1", manifest)
    repo.save_manifest("run-1", manifest)
    with pytest.raises(ValueError, match="immutable"):
        repo.save_manifest("run-1", {"manifest_hash": "different", "datasets": {}})
    assert repo.finalize("run-1") is True
    assert repo.get_record("run-1")["status"] == "finalized"
    with pytest.raises(ValueError, match="immutable"):
        repo.save_manifest("run-1", manifest)
    with pytest.raises(ValueError, match="immutable"):
        repo.save_manifest("run-2", manifest, status="finalized")
    store.close()


def test_run_and_report_repositories_have_minimal_adapters(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    runs = AnalysisRunRepository(store)
    runs.upsert({"run_id": "r1", "request_json": "{}", "status": "completed", "progress": 1.0})
    assert runs.get("r1")["status"] == "completed"
    report_dir = tmp_path / "reports" / "r1"
    report_dir.mkdir(parents=True)
    (report_dir / "complete_report.md").write_text("# report", encoding="utf-8")
    (report_dir / "run.json").write_text('{"status":"completed"}', encoding="utf-8")
    (report_dir / "COMMITTED").write_text("ok", encoding="utf-8")
    reports = ReportRepository(store)
    assert reports.is_gate_ready(report_dir)
    assert list(reports.iter_ready(tmp_path / "reports")) == [report_dir]
    store.close()


def test_run_repository_round_trips_lifecycle_fields_and_json_lists(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = AnalysisRunRepository(store)
    repo.upsert(
        {
            "run_id": "lifecycle",
            "request_json": "{}",
            "status": "timed_out",
            "progress": 0.45,
            "error_code": "heartbeat_timeout",
            "last_heartbeat_at": "2026-08-27T00:00:00+00:00",
            "timeout_at": "2026-08-27T02:00:00+00:00",
            "run_timeout_seconds": 7200,
            "run_heartbeat_interval_seconds": 15,
            "run_heartbeat_timeout_seconds": 180,
            "effective_quote_provider_chain": ["yfinance", "alpha_vantage"],
        }
    )
    record = repo.get("lifecycle")
    assert record["terminal_reason"] == record["error_code"] == "heartbeat_timeout"
    assert record["effective_quote_provider_chain"] == ["yfinance", "alpha_vantage"]
    assert repo.list(status="timed_out")[0] == record
    store.close()


def test_settings_allow_run_lifecycle_configuration(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = SettingsRepository(store)
    for key, value in (
        ("run_timeout_seconds", 3600),
        ("run_heartbeat_interval_seconds", 20),
        ("run_heartbeat_timeout_seconds", 120),
    ):
        repo.set(key, value)
        assert repo.get(key) == {"value": str(value), "source": "sqlite"}
    store.close()
