"""Runner integration: artifact persistence and provider failure classification."""

from __future__ import annotations

from pathlib import Path

from web.artifacts import ArtifactRepository
from web.error_codes import TerminalReason
from web.manager import RunManager
from web.models import AnalysisRequest, RunStatus
from web.runner import WebRunRunner
from web.storage import SQLiteStore


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


class _ChunkGraph:
    """Emits a single market_report chunk then succeeds."""

    def __init__(self, analysts, *, config, debug):
        self.config = config
        self.deep_thinking_llm = None
        self.signature = config.get("checkpoint_signature")

    def propagate(self, ticker, date, *, asset_type, on_chunk, should_cancel):
        on_chunk({"market_report": "market body"})
        return ({"market_report": "market body", "final_trade_decision": "BUY"}, "BUY")

    def save_reports(self, state, ticker, *, save_path):
        path = Path(save_path)
        path.mkdir(parents=True)
        (path / "complete_report.md").write_text("report", encoding="utf-8")


class _CrashGraph:
    """Raises an OpenAI-style timeout exception during propagation."""

    def __init__(self, analysts, *, config, debug):
        self.config = config
        self.deep_thinking_llm = None

    def propagate(self, ticker, date, *, asset_type, on_chunk, should_cancel):
        on_chunk({"market_report": "in progress..."})
        # Mimic an OpenAI SDK APITimeoutError on the second call.
        class _Timeout(Exception):
            pass
        raise _Timeout("timed out after 120s")


def test_runner_persists_completed_artifact_for_each_chunk(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    repo = ArtifactRepository(store)
    manager = RunManager(store=store)
    manager.attach_artifact_repository(repo)
    run = manager.start_run(_request(), run_id="run-1")
    manager.begin_run(run.run_id)
    WebRunRunner(
        manager,
        graph_factory=_ChunkGraph,
        config={"results_dir": str(tmp_path)},
        artifact_repository=repo,
    ).worker(run.run_id)
    snapshot = manager.get_run(run.run_id)
    assert snapshot.status is RunStatus.COMPLETED
    assert snapshot.artifact_count >= 1
    assert snapshot.completed_artifact_count >= 1
    artifacts = manager.list_artifacts(run.run_id)
    keys = {a["artifact_key"] for a in artifacts}
    assert "market_report" in keys
    market = next(a for a in artifacts if a["artifact_key"] == "market_report")
    assert market["status"] == "completed"
    assert market["artifact_type"] == "analyst_report"
    assert market["agent"] == "Market Analyst"
    manager.shutdown()
    store.close()


def test_runner_classifies_provider_timeout_and_persists_failure(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    repo = ArtifactRepository(store)
    manager = RunManager(store=store)
    manager.attach_artifact_repository(repo)
    run = manager.start_run(_request(), run_id="run-2")
    manager.begin_run(run.run_id)
    WebRunRunner(
        manager,
        graph_factory=_CrashGraph,
        config={"results_dir": str(tmp_path)},
        artifact_repository=repo,
    ).worker(run.run_id)
    snapshot = manager.get_run(run.run_id)
    assert snapshot.status is RunStatus.FAILED
    assert snapshot.terminal_reason == TerminalReason.MODEL_TIMEOUT.value
    assert snapshot.failed_phase == "Analyst Team"
    # The partial artifact that streamed before the timeout is preserved.
    artifacts = manager.list_artifacts(run.run_id)
    keys = [a["artifact_key"] for a in artifacts]
    assert keys == ["market_report"]
    assert artifacts[0]["status"] == "partial"
    manager.shutdown()
    store.close()


def test_runner_attaches_checkpoint_signature_to_record(tmp_path):
    """Phase-2: the signature computed by the runner lands on the row."""

    store = SQLiteStore(tmp_path / "db.sqlite3")
    repo = ArtifactRepository(store)
    manager = RunManager(store=store)
    manager.attach_artifact_repository(repo)
    run = manager.start_run(_request(), run_id="run-3")
    manager.begin_run(run.run_id)
    WebRunRunner(
        manager,
        graph_factory=_ChunkGraph,
        config={"results_dir": str(tmp_path)},
        artifact_repository=repo,
    ).worker(run.run_id)
    snapshot = manager.get_run(run.run_id)
    # The resume_checkpoint_id is set to the signature so the retry API
    # can validate compatibility later.
    assert snapshot.resume_checkpoint_id is not None
    assert "analysts=market" in snapshot.resume_checkpoint_id
    manager.shutdown()
    store.close()
