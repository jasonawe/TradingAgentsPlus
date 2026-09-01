import pytest

from web.repositories import (
    AnalysisRunRepository,
    ProviderHealthRepository,
    QuoteRepository,
    ReportIndexRepository,
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


def _report_metadata(report_id="r1", **overrides):
    value = {
        "report_id": report_id,
        "run_id": report_id,
        "ticker": "AAPL",
        "asset_type": "stock",
        "analysis_date": "2026-08-27",
        "generated_at": "2026-08-27T10:00:00+00:00",
        "status": "completed",
        "rating": "Hold",
        "decision_preview": "维持持有",
        "analysts": ["market", "news"],
        "effective_quote_provider_chain": ["yfinance"],
        "root_name": "web_reports",
        "relative_path": f"AAPL/2026-08-27/{report_id}",
        "source": "web",
        "index_status": "indexed",
        "path_state": "valid",
    }
    value.update(overrides)
    return value


def test_report_index_round_trips_metadata_and_rejects_unsafe_paths(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = ReportIndexRepository(store)
    repo.upsert(_report_metadata(decision_preview="结论" * 400))

    record = repo.get("r1")
    assert record["rating"] == record["signal"] == "Hold"
    assert record["analysts"] == ["market", "news"]
    assert record["effective_quote_provider_chain"] == ["yfinance"]
    assert len(record["decision_preview"]) == 512

    with pytest.raises(ValueError, match="root"):
        repo.upsert(_report_metadata("bad-root", root_name="outside"))
    with pytest.raises(ValueError, match="path"):
        repo.upsert(_report_metadata("bad-path", relative_path="../../secret"))
    with pytest.raises(ValueError, match="status"):
        repo.upsert(_report_metadata("bad-status", status="running"))
    store.close()


def test_report_index_outbox_overlays_before_filter_sort_and_retry(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = ReportIndexRepository(store)
    repo.upsert(_report_metadata("old", generated_at=None, ticker="MSFT"))
    repo.upsert(_report_metadata("same", decision_preview="旧结论"))
    repo.enqueue(
        _report_metadata(
            "same",
            ticker="AAPL",
            decision_preview="新结论包含增长",
            generated_at="2026-08-28T10:00:00+00:00",
        ),
        "database busy",
    )

    page = repo.search(
        page=1,
        page_size=10,
        query="增长",
        ticker=None,
        status="completed",
        asset_type="stock",
        date_from=None,
        date_to=None,
        sort="generated_at_desc",
    )
    assert page["total"] == 1
    assert [item["report_id"] for item in page["items"]] == ["same"]
    assert page["items"][0]["decision_preview"] == "新结论包含增长"
    assert page["items"][0]["analysts"] == ["market", "news"]
    assert page["items"][0]["effective_quote_provider_chain"] == ["yfinance"]

    assert repo.retry_outbox(limit=10) == 1
    assert repo.get("same")["decision_preview"] == "新结论包含增长"
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM report_index_outbox").fetchone()[0] == 0
    store.close()


def test_report_index_sort_keeps_null_generated_time_last_with_stable_tie_breaker(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    repo = ReportIndexRepository(store)
    for report_id, generated in (
        ("b", "2026-08-27T10:00:00+00:00"),
        ("a", "2026-08-27T10:00:00+00:00"),
        ("z", None),
    ):
        repo.upsert(_report_metadata(report_id, generated_at=generated))
    descending = repo.search(page=1, page_size=10, sort="generated_at_desc")
    ascending = repo.search(page=1, page_size=10, sort="generated_at_asc")
    assert [item["report_id"] for item in descending["items"]] == ["a", "b", "z"]
    assert [item["report_id"] for item in ascending["items"]] == ["a", "b", "z"]
    store.close()


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def test_provider_health_persists_window_failures_and_resets_at_five_minutes(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = SQLiteStore(tmp_path / "health.sqlite3")
    clock = MutableClock(datetime(2026, 8, 27, tzinfo=timezone.utc))
    repo = ProviderHealthRepository(store, clock=clock)
    assert repo.mark_not_configured("alpha_vantage", "missing key")["status"] == "not_configured"

    for _ in range(3):
        repo.record_failure("yfinance", "timeout", "token=secret provider down", 120.5)
    degraded = repo.get("yfinance")
    assert degraded["status"] == "degraded"
    assert degraded["request_count"] == degraded["failure_count"] == 3
    assert degraded["consecutive_failures"] == 3
    assert "secret" not in degraded["last_error_message"]
    assert len(degraded["last_error_message"]) <= 512

    ready = repo.record_success("yfinance", 20.0)
    assert ready["status"] == "ready"
    assert ready["consecutive_failures"] == 0
    persisted = ProviderHealthRepository(store, clock=clock).get("yfinance")
    assert persisted["last_latency_ms"] == 20.0

    clock.value += timedelta(seconds=300)
    reset = repo.record_success("yfinance", 10.0)
    assert reset["request_count"] == 1
    assert reset["failure_count"] == 0
    assert reset["consecutive_failures"] == 0
    store.close()


def test_provider_health_symbol_outcomes_do_not_degrade_and_permanent_error_does(tmp_path):
    from datetime import datetime, timezone

    repo = ProviderHealthRepository(
        SQLiteStore(tmp_path / "health.sqlite3"),
        clock=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
    )
    for code in ("no_data", "invalid_symbol"):
        record = repo.record_failure("yfinance", code, code, 1.0)
        assert record["status"] == "ready"
        assert record["failure_count"] == 0
    permanent = repo.record_failure(
        "yfinance", "provider_error", "invalid adapter", 2.0, permanent=True
    )
    assert permanent["status"] == "error"
