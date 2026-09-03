import json
import sqlite3

import pytest

from web.repositories import (
    AnalysisRunRepository,
    ScheduledJobRepository,
    ScheduledRunLogRepository,
)
from web.storage import SQLiteStore


def _request(symbol: str, *, asset_type: str = "stock", provider: str = "openai"):
    return {
        "ticker": symbol,
        "analysis_date": "2026-09-01",
        "asset_type": asset_type,
        "analysts": ["market", "news"],
        "research_depth": 3,
        "output_language": "Chinese",
        "provider": provider,
        "quick_model": "quick-test",
        "deep_model": "deep-test",
        "quote_strategy_id": "fallback-yfinance-alpha-vantage",
    }


def test_scheduled_migration_creates_tables_indexes_and_cascade(tmp_path):
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    assert store.schema_version == 5

    with store.connection() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"scheduled_jobs", "scheduled_run_logs"} <= tables
        assert {"idx_scheduled_jobs_enabled", "idx_scheduled_run_logs_job"} <= indexes

        conn.execute(
            "INSERT INTO scheduled_jobs(id,symbol,asset_type,cron_expression,enabled,created_at,updated_at) "
            "VALUES ('job-1','AAPL','stock','0 9 * * 1-5',1,'now','now')"
        )
        conn.execute(
            "INSERT INTO scheduled_run_logs(id,job_id,symbol,asset_type,scheduled_for,fired_at,status) "
            "VALUES ('log-1','job-1','AAPL','stock','now','now','queued')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO scheduled_jobs(id,symbol,asset_type,cron_expression,enabled,created_at,updated_at) "
                "VALUES ('job-2','AAPL','stock','30 9 * * 1-5',1,'now','now')"
            )
        conn.execute("DELETE FROM scheduled_jobs WHERE id='job-1'")
        assert conn.execute("SELECT COUNT(*) FROM scheduled_run_logs").fetchone()[0] == 0
    store.close()


def test_scheduled_job_repository_crud_and_toggle(tmp_path):
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    repo = ScheduledJobRepository(store)

    created = repo.create(
        "aapl",
        asset_type="stock",
        cron_expression="0 9 * * 1-5",
        note="opening brief",
    )
    assert created["symbol"] == "AAPL"
    assert created["enabled"] is True
    assert repo.get(created["id"]) == created
    assert repo.get_by_asset("AAPL", "stock")["id"] == created["id"]
    assert [item["id"] for item in repo.list()] == [created["id"]]

    updated = repo.update(
        created["id"],
        cron_expression="30 9 * * 1-5",
        note=None,
    )
    assert updated["cron_expression"] == "30 9 * * 1-5"
    assert updated["note"] is None
    assert repo.toggle(created["id"], False)["enabled"] is False
    assert repo.list(enabled=True) == []

    with pytest.raises(ValueError, match="already exists"):
        repo.create("AAPL", asset_type="stock", cron_expression="0 10 * * *")

    repo.delete(created["id"])
    assert repo.list() == []
    with pytest.raises(KeyError):
        repo.get(created["id"])
    store.close()


def test_scheduled_job_repository_rejects_invalid_cron_on_create_and_update(tmp_path):
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    repo = ScheduledJobRepository(store)

    with pytest.raises(ValueError, match="exactly 5 fields"):
        repo.create("AAPL", asset_type="stock", cron_expression="0 0 9 * * 1-5")

    job = repo.create("AAPL", asset_type="stock", cron_expression="0 9 * * 1-5")
    with pytest.raises(ValueError, match="invalid cron expression"):
        repo.update(job["id"], cron_expression="61 9 * * 1-5")
    assert repo.get(job["id"])["cron_expression"] == "0 9 * * 1-5"
    store.close()


def test_scheduled_run_log_repository_updates_and_joins_report_id(tmp_path):
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    jobs = ScheduledJobRepository(store)
    logs = ScheduledRunLogRepository(store)
    job = jobs.create("MSFT", asset_type="stock", cron_expression="0 9 * * *")

    with store.connection() as conn:
        conn.execute(
            "INSERT INTO web_runs(run_id,request_json,status,progress,report_id,finished_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                "run-1",
                json.dumps(_request("MSFT")),
                "completed",
                1.0,
                "report-1",
                "2026-09-02T01:00:00+00:00",
            ),
        )

    created = logs.create(
        job["id"],
        symbol="MSFT",
        asset_type="stock",
        scheduled_for="2026-09-02T09:00:00+08:00",
        fired_at="2026-09-02T09:00:01+08:00",
        status="queued",
        run_id="run-1",
        parameter_source="last_successful",
    )
    updated = logs.update(created["id"], status="succeeded", error=None)
    assert updated["status"] == "succeeded"
    assert updated["report_id"] == "report-1"
    assert logs.get(created["id"])["report_id"] == "report-1"
    assert [item["id"] for item in logs.list(job["id"], limit=1)] == [created["id"]]
    store.close()


def test_latest_successful_request_skips_failed_other_asset_and_malformed_json(tmp_path):
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    with store.connection() as conn:
        rows = [
            ("old", json.dumps(_request("aapl", provider="openai")), "completed", "2026-09-01T01:00:00+00:00"),
            ("failed", json.dumps(_request("AAPL", provider="anthropic")), "failed", "2026-09-02T01:00:00+00:00"),
            ("other", json.dumps(_request("AAPL", asset_type="crypto")), "completed", "2026-09-03T01:00:00+00:00"),
            ("broken", "{not json", "completed", "2026-09-06T01:00:00+00:00"),
            ("new", json.dumps(_request("AAPL", provider="google")), "completed", "2026-09-05T01:00:00+00:00"),
        ]
        conn.executemany(
            "INSERT INTO web_runs(run_id,request_json,status,progress,finished_at) VALUES (?,?,?,?,?)",
            [(run_id, request_json, status, 1.0, finished_at) for run_id, request_json, status, finished_at in rows],
        )

    request = AnalysisRunRepository(store).latest_successful_request("aapl", "stock")
    assert request is not None
    assert request["ticker"] == "AAPL"
    assert request["provider"] == "google"
    assert AnalysisRunRepository(store).latest_successful_request("NVDA", "stock") is None
    store.close()


def test_latest_successful_request_orders_offset_timestamps_by_actual_instant(tmp_path):
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    with store.connection() as conn:
        rows = [
            (
                "lexically-newer",
                json.dumps(_request("AAPL", provider="openai")),
                "2026-09-02T08:00:00+09:00",
            ),
            (
                "actually-newer",
                json.dumps(_request("AAPL", provider="google")),
                "2026-09-02T00:30:00+00:00",
            ),
            ("missing-time", json.dumps(_request("AAPL", provider="anthropic")), None),
        ]
        conn.executemany(
            "INSERT INTO web_runs(run_id,request_json,status,progress,finished_at) "
            "VALUES (?,?,'completed',1.0,?)",
            rows,
        )

    request = AnalysisRunRepository(store).latest_successful_request("AAPL", "stock")
    assert request is not None
    assert request["provider"] == "google"
    store.close()


def test_latest_successful_request_ignores_unparseable_completion_time(tmp_path):
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO web_runs(run_id,request_json,status,progress,finished_at) "
            "VALUES (?,?, 'completed',1.0,?)",
            ("broken-time", json.dumps(_request("AAPL")), "not-a-time"),
        )

    assert AnalysisRunRepository(store).latest_successful_request("AAPL", "stock") is None
    store.close()


def test_latest_successful_request_preserves_microsecond_ordering(tmp_path):
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    with store.connection() as conn:
        conn.executemany(
            "INSERT INTO web_runs(run_id,request_json,status,progress,finished_at) "
            "VALUES (?,?,'completed',1.0,?)",
            [
                (
                    "older",
                    json.dumps(_request("AAPL", provider="openai")),
                    "2026-09-02T00:30:00.000001+00:00",
                ),
                (
                    "newer",
                    json.dumps(_request("AAPL", provider="google")),
                    "2026-09-02T00:30:00.000002+00:00",
                ),
            ],
        )

    request = AnalysisRunRepository(store).latest_successful_request("AAPL", "stock")
    assert request is not None
    assert request["provider"] == "google"
    store.close()


def test_latest_successful_request_skips_newer_invalid_request_shape(tmp_path):
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    invalid = _request("AAPL", provider="google")
    invalid["research_depth"] = 2
    with store.connection() as conn:
        conn.executemany(
            "INSERT INTO web_runs(run_id,request_json,status,progress,finished_at) "
            "VALUES (?,?,'completed',1.0,?)",
            [
                (
                    "a-valid-older",
                    json.dumps(_request("AAPL", provider="openai")),
                    "2026-09-02T00:30:00.000001+00:00",
                ),
                (
                    "z-invalid-newer",
                    json.dumps(invalid),
                    "2026-09-02T00:30:00.000002+00:00",
                ),
            ],
        )

    request = AnalysisRunRepository(store).latest_successful_request("AAPL", "stock")
    assert request is not None
    assert request["provider"] == "openai"
    assert request["research_depth"] == 3
    store.close()


def test_latest_successful_request_skips_utc_conversion_overflow(tmp_path):
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    with store.connection() as conn:
        conn.executemany(
            "INSERT INTO web_runs(run_id,request_json,status,progress,finished_at) "
            "VALUES (?,?,'completed',1.0,?)",
            [
                (
                    "valid",
                    json.dumps(_request("AAPL", provider="openai")),
                    "2026-09-02T00:30:00+00:00",
                ),
                (
                    "overflowing-other-asset",
                    json.dumps(_request("MSFT", provider="google")),
                    "0001-01-01T00:00:00+14:00",
                ),
            ],
        )

    request = AnalysisRunRepository(store).latest_successful_request("AAPL", "stock")
    assert request is not None
    assert request["provider"] == "openai"
    store.close()
