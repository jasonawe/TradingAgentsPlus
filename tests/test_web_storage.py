import sqlite3
from pathlib import Path

import pytest

from web.storage import SQLiteStore


def test_store_migrates_schema_and_preserves_existing_web_runs(tmp_path):
    db = tmp_path / "web.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE web_runs (run_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, status TEXT NOT NULL, phase TEXT, current_agent TEXT, progress REAL NOT NULL, queued_at TEXT, started_at TEXT, finished_at TEXT, signal TEXT, report_id TEXT, error_code TEXT, error_message TEXT, terminal_expires_at TEXT)"
        )
        conn.execute("INSERT INTO web_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("r1", "{}", "completed", None, None, 1.0, None, None, None, "BUY", "rep", None, None, None))
    store = SQLiteStore(db)
    assert store.schema_version == 3
    with store.connection() as conn:
        row = conn.execute(
            "SELECT report_id,last_heartbeat_at,timeout_at,terminal_reason,"
            "run_timeout_seconds,run_heartbeat_interval_seconds,"
            "run_heartbeat_timeout_seconds FROM web_runs WHERE run_id='r1'"
        ).fetchone()
        assert tuple(row) == ("rep", None, None, None, None, None, None)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "schema_version",
            "web_runs",
            "watchlists",
            "watchlist_items",
            "market_quotes",
            "market_candles",
            "analysis_data_snapshots",
            "settings",
            "web_run_events",
            "reports",
            "report_index_outbox",
            "provider_health",
        } <= tables
    store.close()


def test_v2_schema_matches_the_reliability_contract(tmp_path):
    store = SQLiteStore(tmp_path / "web.sqlite3")
    expected = {
        "reports": {
            "report_id", "run_id", "ticker", "asset_type", "analysis_date",
            "generated_at", "status", "rating", "signal", "output_language",
            "summary_status", "decision_preview", "data_snapshot_id", "provider",
            "quick_model", "deep_model", "analysts_json", "research_depth",
            "data_status", "reproducibility", "quote_strategy_id",
            "effective_quote_provider_chain", "root_name", "relative_path", "source",
            "index_status", "path_state", "updated_at",
        },
        "report_index_outbox": {
            "report_id", "root_name", "relative_path", "payload_json", "attempts",
            "last_error", "updated_at",
        },
        "provider_health": {
            "provider", "status", "window_started_at", "request_count",
            "failure_count", "consecutive_failures", "last_success_at",
            "last_failure_at", "last_latency_ms", "last_error_code",
            "last_error_message", "updated_at",
        },
    }
    with store.connection() as conn:
        for table, columns in expected.items():
            assert {row[1] for row in conn.execute(f"PRAGMA table_info({table})")} == columns
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(reports)")}
        assert {
            "idx_reports_generated",
            "idx_reports_ticker",
            "idx_reports_status",
            "idx_reports_analysis_date",
        } <= indexes
    store.close()


def test_v3_reorders_existing_watchlist_items_newest_first(tmp_path):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    source_dir = Path(__file__).parents[1] / "web" / "migrations"
    for name in ("001_personal_platform.sql", "002_reliability_operations.sql"):
        source = source_dir / name
        (migration_dir / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    db = tmp_path / "web.sqlite3"
    v2_store = SQLiteStore(db, migrations_dir=migration_dir)
    with v2_store.connection() as conn:
        conn.executemany(
            "INSERT INTO watchlist_items("
            "id,watchlist_id,symbol,asset_type,position,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?)",
            [
                ("old", "default", "AAPL", "stock", 0, "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
                ("new", "default", "MSFT", "stock", 1, "2026-08-02T00:00:00+00:00", "2026-08-02T00:00:00+00:00"),
            ],
        )
    v2_store.close()

    source_v3 = source_dir / "003_watchlist_newest_first.sql"
    (migration_dir / source_v3.name).write_text(source_v3.read_text(encoding="utf-8"), encoding="utf-8")
    upgraded = SQLiteStore(db, migrations_dir=migration_dir)
    with upgraded.connection() as conn:
        rows = conn.execute(
            "SELECT symbol,position FROM watchlist_items ORDER BY position,id"
        ).fetchall()
    assert upgraded.schema_version == 3
    assert [tuple(row) for row in rows] == [("MSFT", 0), ("AAPL", 1)]
    upgraded.close()


def test_failed_migration_rolls_back(tmp_path):
    db = tmp_path / "web.sqlite3"
    store = SQLiteStore(db, migrations_dir=tmp_path / "migrations")
    store.close()
    # A fresh store with an invalid migration must leave schema_version at 0.
    bad = tmp_path / "bad.sqlite3"
    migration_dir = tmp_path / "bad-migrations"
    migration_dir.mkdir()
    (migration_dir / "001_personal_platform.sql").write_text("CREATE TABLE broken (x TEXT); THIS IS INVALID;", encoding="utf-8")
    with pytest.raises(sqlite3.Error):
        SQLiteStore(bad, migrations_dir=migration_dir)
    with sqlite3.connect(bad) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='broken'").fetchone() is None
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 0


def test_v2_python_hook_and_sql_roll_back_as_one_transaction(tmp_path):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    source_v1 = Path(__file__).parents[1] / "web" / "migrations" / "001_personal_platform.sql"
    (migration_dir / source_v1.name).write_text(source_v1.read_text(encoding="utf-8"), encoding="utf-8")
    v1_store = SQLiteStore(tmp_path / "web.sqlite3", migrations_dir=migration_dir)
    assert v1_store.schema_version == 1
    v1_store.close()

    (migration_dir / "002_reliability_operations.sql").write_text(
        "CREATE TABLE should_rollback (value TEXT); THIS IS INVALID;",
        encoding="utf-8",
    )
    with pytest.raises(sqlite3.Error):
        SQLiteStore(tmp_path / "web.sqlite3", migrations_dir=migration_dir)

    with sqlite3.connect(tmp_path / "web.sqlite3") as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        columns = {row[1] for row in conn.execute("PRAGMA table_info(web_runs)")}
        assert "last_heartbeat_at" not in columns
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='should_rollback'"
        ).fetchone() is None


def test_migration_parser_handles_semicolons_in_strings_and_comments(tmp_path):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    (migration_dir / "001_personal_platform.sql").write_text(
        "-- semicolon ; in comment\nCREATE TABLE sample (value TEXT);\nINSERT INTO sample(value) VALUES ('a;b');\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "web.sqlite3", migrations_dir=migration_dir)
    with store.connection() as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "a;b"
    store.close()


def test_legacy_snapshots_table_is_renamed_without_parallel_storage(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (1)")
        conn.execute("CREATE TABLE snapshots (run_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL, manifest_hash TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT)")
        conn.execute("INSERT INTO snapshots VALUES ('r1','{}','h','recording','now',NULL)")
    store = SQLiteStore(db)
    with store.connection() as conn:
        assert conn.execute("SELECT run_id FROM analysis_data_snapshots").fetchone()[0] == "r1"
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='snapshots'").fetchone() is None
    store.close()


def test_old_web_runs_missing_columns_are_upgraded_and_loaded(tmp_path):
    db = tmp_path / "old.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE web_runs (run_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL)")
        conn.execute("INSERT INTO web_runs VALUES ('r1', '{\"ticker\":\"AAPL\",\"analysis_date\":\"2026-08-27\",\"analysts\":[\"market\"],\"research_depth\":1}', 'completed', 1.0)")
    store = SQLiteStore(db)
    with store.connection() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(web_runs)")}
    assert "terminal_expires_at" in columns
    store.close()


def test_snapshots_only_database_merges_legacy_data(tmp_path):
    db = tmp_path / "snapshots-only.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE snapshots (run_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL, manifest_hash TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT)")
        conn.execute("INSERT INTO snapshots VALUES ('r1','{\"manifest_hash\":\"h\"}','h','recording','now',NULL)")
    store = SQLiteStore(db)
    from web.repositories import SnapshotRepository
    assert SnapshotRepository(store).get_manifest("r1")["manifest_hash"] == "h"
    with store.connection() as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "analysis_data_snapshots" in names and "snapshots" not in names
    store.close()


def test_app_manager_uses_the_same_store_boundary(tmp_path):
    from web.app import create_app

    app = create_app(config={"results_dir": str(tmp_path), "project_dir": str(tmp_path)})
    assert app.state.manager._store is app.state.store
    app.state.manager.shutdown()
    app.state.store.close()


def test_app_with_injected_manager_reuses_its_store(tmp_path):
    from web.app import create_app
    from web.manager import RunManager

    manager = RunManager(db_path=tmp_path / "manager.sqlite3")
    app = create_app(manager=manager, config={"results_dir": str(tmp_path), "project_dir": str(tmp_path)})
    assert app.state.store.path == manager._db_path
    assert app.state.manager._db_path == app.state.store.path
    manager.shutdown()
    app.state.store.close()


def test_in_memory_manager_is_attached_to_app_store_and_persists(tmp_path):
    from web.app import create_app
    from web.manager import RunManager

    manager = RunManager()
    app = create_app(manager=manager, config={"results_dir": str(tmp_path), "project_dir": str(tmp_path)})
    run = manager.start_run(__import__("web.models", fromlist=["AnalysisRequest"]).AnalysisRequest(ticker="AAPL", analysis_date="2026-08-27", analysts=["market"], research_depth=1), run_id="attached")
    manager.shutdown()
    restored = RunManager(db_path=app.state.store.path)
    assert restored.get_run(run.run_id).run_id == "attached"
    restored.shutdown()
    app.state.store.close()
