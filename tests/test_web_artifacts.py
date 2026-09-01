"""Artifact persistence: idempotent upsert, partial vs completed, failure retention."""

from __future__ import annotations

import pytest

from web.artifacts import (
    VALID_ARTIFACT_STATUSES,
    VALID_ARTIFACT_TYPES,
    ArtifactRepository,
)
from web.manager import RunManager
from web.models import AnalysisRequest, RunStatus
from web.storage import SQLiteStore


def _request():
    return AnalysisRequest(
        ticker="AAPL",
        analysis_date="2026-08-26",
        asset_type="stock",
        analysts=["market"],
        research_depth=1,
    )


def test_artifact_types_and_statuses_are_whitelisted():
    assert {"analyst_report", "research_debate", "trader_plan"} <= VALID_ARTIFACT_TYPES
    assert frozenset({"partial", "completed"}) == VALID_ARTIFACT_STATUSES


def test_repository_rejects_unknown_artifact_type(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    repo = ArtifactRepository(store)
    manager = RunManager(store=store)
    run = manager.start_run(_request(), run_id="r1")
    with pytest.raises(ValueError):
        repo.upsert(
            run.run_id,
            artifact_key="bad",
            artifact_type="bogus",
            phase="Analyst Team",
            title="t",
            content_markdown="x",
            status="completed",
            sequence=0,
        )
    manager.shutdown()
    store.close()


def test_upsert_is_idempotent_and_preserves_created_at(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    repo = ArtifactRepository(store)
    manager = RunManager(store=store)
    run = manager.start_run(_request(), run_id="run-1")
    first = repo.upsert(
        run.run_id,
        artifact_key="market_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="Market Analyst",
        title="Market Analyst report",
        content_markdown="initial text",
        status="partial",
        sequence=0,
    )
    second = repo.upsert(
        run.run_id,
        artifact_key="market_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="Market Analyst",
        title="Market Analyst report",
        content_markdown="expanded text",
        status="completed",
        sequence=0,
    )
    assert first.artifact_key == second.artifact_key == "market_report"
    assert first.created_at == second.created_at
    assert second.content_markdown == "expanded text"
    assert second.status == "completed"
    # Only one row stored.
    listed = repo.list_for_run(run.run_id)
    assert [record.artifact_key for record in listed] == ["market_report"]
    manager.shutdown()
    store.close()


def test_list_orders_by_sequence_and_returns_latest_state(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    repo = ArtifactRepository(store)
    manager = RunManager(store=store)
    run = manager.start_run(_request(), run_id="run-1")
    repo.upsert(
        run.run_id,
        artifact_key="news_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="News Analyst",
        title="News",
        content_markdown="news body",
        status="completed",
        sequence=1,
    )
    repo.upsert(
        run.run_id,
        artifact_key="market_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="Market Analyst",
        title="Market",
        content_markdown="market body",
        status="completed",
        sequence=0,
    )
    repo.upsert(
        run.run_id,
        artifact_key="fundamentals_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="Fundamentals Analyst",
        title="Fundamentals",
        content_markdown="...",
        status="partial",
        sequence=2,
    )
    listed = repo.list_for_run(run.run_id)
    assert [record.artifact_key for record in listed] == [
        "market_report",
        "news_report",
        "fundamentals_report",
    ]
    counts = repo.counts(run.run_id)
    assert counts == {
        "artifact_count": 3,
        "completed_artifact_count": 2,
        "partial_artifact_count": 1,
    }
    manager.shutdown()
    store.close()


def test_get_run_populates_artifact_counts(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    repo = ArtifactRepository(store)
    manager = RunManager(store=store)
    manager.attach_artifact_repository(repo)
    run = manager.start_run(_request(), run_id="r-counts")
    repo.upsert(
        run.run_id,
        artifact_key="market_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="Market Analyst",
        title="Market",
        content_markdown="x",
        status="completed",
        sequence=0,
    )
    repo.upsert(
        run.run_id,
        artifact_key="news_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="News Analyst",
        title="News",
        content_markdown="...",
        status="partial",
        sequence=1,
    )
    snapshot = manager.get_run(run.run_id)
    assert snapshot.artifact_count == 2
    assert snapshot.completed_artifact_count == 1
    assert snapshot.has_partial_results is True
    manager.shutdown()
    store.close()


def test_artifacts_survive_a_failed_run_and_remain_readable(tmp_path):
    """Phase-1 acceptance: partial reports survive a failed terminal."""

    store = SQLiteStore(tmp_path / "db.sqlite3")
    repo = ArtifactRepository(store)
    manager = RunManager(store=store)
    manager.attach_artifact_repository(repo)
    run = manager.start_run(_request(), run_id="r-failed")
    manager.begin_run(run.run_id)
    repo.upsert(
        run.run_id,
        artifact_key="market_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="Market Analyst",
        title="Market",
        content_markdown="complete",
        status="completed",
        sequence=0,
    )
    repo.upsert(
        run.run_id,
        artifact_key="news_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="News Analyst",
        title="News",
        content_markdown="...",
        status="partial",
        sequence=1,
    )
    manager.fail_run(
        run.run_id,
        error_code="model_timeout",
        error_message="model timed out",
        failed_phase="Research Team",
    )
    snapshot = manager.get_run(run.run_id)
    assert snapshot.status is RunStatus.FAILED
    artifacts = manager.list_artifacts(run.run_id)
    keys = [a["artifact_key"] for a in artifacts]
    assert keys == ["market_report", "news_report"]
    manager.shutdown()
    store.close()


def test_partial_artifact_is_not_promoted_to_completed(tmp_path):
    """Upserts that send partial content must keep status='partial'."""

    store = SQLiteStore(tmp_path / "db.sqlite3")
    repo = ArtifactRepository(store)
    manager = RunManager(store=store)
    run = manager.start_run(_request(), run_id="run-partial")
    repo.upsert(
        run.run_id,
        artifact_key="news_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="News Analyst",
        title="News",
        content_markdown="in progress...",
        status="partial",
        sequence=0,
    )
    later = repo.upsert(
        run.run_id,
        artifact_key="news_report",
        artifact_type="analyst_report",
        phase="Analyst Team",
        agent="News Analyst",
        title="News",
        content_markdown="in progress ",
        status="partial",
        sequence=0,
    )
    assert later.status == "partial"
    manager.shutdown()
    store.close()
