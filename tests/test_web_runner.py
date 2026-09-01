import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from web.manager import RunManager
from web.models import AnalysisRequest, EventName, RunStatus
from web.runner import WebRunRunner


def request(**overrides):
    value = {
        "ticker": "AAPL", "analysis_date": "2026-08-26", "asset_type": "stock",
        "analysts": ["market"], "research_depth": 1,
    }
    value.update(overrides)
    return AnalysisRequest(**value)


class FakeGraph:
    def __init__(self, analysts, *, config, debug):
        FakeGraph.last_instance = self
        FakeGraph.last_config = config
        self.config = config
        self.debug = debug
        self.deep_thinking_llm = None

    def propagate(self, ticker, date, *, asset_type, on_chunk, should_cancel):
        supplier = self.config.get("deadline_supplier")
        FakeGraph.last_remaining_deadline = supplier() if callable(supplier) else None
        on_chunk({"market_report": "report"})
        return ({"market_report": "report", "final_trade_decision": "BUY"}, "BUY")

    def save_reports(self, state, ticker, *, save_path):
        path = Path(save_path)
        path.mkdir(parents=True)
        (path / "complete_report.md").write_text("report", encoding="utf-8")


class TrackingHistory:
    def __init__(self):
        self.indexed = []

    def index_report(self, path):
        self.indexed.append(Path(path))


def test_runner_streams_events_and_writes_sidecar(tmp_path):
    manager = RunManager()
    run = manager.start_run(request(), run_id="run-1")
    manager.begin_run(run.run_id)
    WebRunRunner(manager, graph_factory=FakeGraph, config={"results_dir": str(tmp_path)}).worker(run.run_id)
    assert manager.get_run(run.run_id).status is RunStatus.COMPLETED
    events = manager.read_events(run.run_id).events
    assert EventName.AGENT_STATUS in [event.event for event in events]
    assert EventName.PROGRESS in [event.event for event in events]
    sidecar = tmp_path / "web_reports" / "AAPL" / "2026-08-26" / "run-1" / "run.json"
    metadata = json.loads(sidecar.read_text())
    assert metadata["run_id"] == metadata["report_id"] == "run-1"
    assert metadata["ticker"] == "AAPL"
    assert metadata["analysis_date"] == "2026-08-26"
    assert metadata["asset_type"] == "stock"
    assert metadata["analysts"] == ["market"]
    assert metadata["research_depth"] == 1
    assert metadata["signal"] == "BUY"
    assert metadata["generated_at"]


def test_runner_applies_request_output_language_to_graph_config(tmp_path):
    manager = RunManager()
    run = manager.start_run(request(output_language="Chinese"), run_id="run-language")
    manager.begin_run(run.run_id)
    WebRunRunner(manager, graph_factory=FakeGraph, config={"results_dir": str(tmp_path), "output_language": "English"}).worker(run.run_id)
    assert manager.get_run(run.run_id).status is RunStatus.COMPLETED
    # The fake graph is instantiated with the per-run language override.
    graph = FakeGraph.last_instance
    assert graph.config["output_language"] == "Chinese"


class SummaryGraph(FakeGraph):
    def __init__(self, analysts, *, config, debug):
        super().__init__(analysts, config=config, debug=debug)
        self.deep_thinking_llm = SummaryLLM()


class SummaryLLM:
    def invoke(self, prompt):
        class Response:
            content = "## Executive summary\n\nHold"
        return Response()


def test_runner_persists_executive_summary_when_deep_model_is_available(tmp_path):
    manager = RunManager()
    run = manager.start_run(request(output_language="Chinese", provider="openai", quick_model="gpt-5.4-mini", deep_model="gpt-5.5"), run_id="run-summary")
    manager.begin_run(run.run_id)
    WebRunRunner(manager, graph_factory=SummaryGraph, config={"results_dir": str(tmp_path)}).worker(run.run_id)
    report_dir = tmp_path / "web_reports" / "AAPL" / "2026-08-26" / "run-summary"
    assert (report_dir / "executive_summary.md").read_text(encoding="utf-8") == "## Executive summary\n\nHold"
    metadata = json.loads((report_dir / "run.json").read_text())
    assert metadata["summary_status"] == "completed"


def test_runner_constrains_unsafe_run_id_path(tmp_path):
    manager = RunManager()
    run = manager.start_run(request(), run_id="../../outside")
    manager.begin_run(run.run_id)
    WebRunRunner(manager, graph_factory=FakeGraph, config={"results_dir": str(tmp_path)}).worker(run.run_id)
    report_dirs = list((tmp_path / "web_reports" / "AAPL" / "2026-08-26").iterdir())
    assert len(report_dirs) == 1
    assert report_dirs[0].name.startswith("run-")
    assert not (tmp_path / "outside").exists()


def test_runner_emits_phase_and_analyst_progress_events(tmp_path):
    manager = RunManager()
    run = manager.start_run(request(), run_id="run-progress")
    manager.begin_run(run.run_id)
    runner = WebRunRunner(manager, graph_factory=FakeGraph, config={"results_dir": str(tmp_path)})
    runner.worker(run.run_id)
    events = manager.read_events(run.run_id).events
    phase_events = [event for event in events if event.event is EventName.PHASE_CHANGED]
    progress_events = [event for event in events if event.event is EventName.PROGRESS]
    statuses = [event.payload.status for event in events if event.event is EventName.AGENT_STATUS]
    assert phase_events[0].payload.phase == "Analyst Team"
    assert progress_events
    assert "in_progress" in statuses
    assert "completed" in statuses


def test_runner_redacts_unknown_activity():
    manager = RunManager()
    run = manager.start_run(request(), run_id="run-2")
    manager.begin_run(run.run_id)
    runner = WebRunRunner(manager)
    runner._publish_chunk(run.run_id, {"unexpected": "api_key=secret"}, ["market"], {"name": "Analyst Team", "index": 1})
    event = manager.read_events(run.run_id).events[-1]
    assert event.event is EventName.ACTIVITY
    assert "secret" not in event.payload.summary


def test_runner_supplies_dynamic_deadline_and_worker_checkpoint_to_graph(tmp_path):
    manager = RunManager()
    run = manager.start_run(request(), run_id="run-deadline")
    manager.begin_run(run.run_id)
    runner = WebRunRunner(
        manager,
        graph_factory=FakeGraph,
        config={"results_dir": str(tmp_path)},
    )
    runner.worker(run.run_id)

    supplier = FakeGraph.last_config["deadline_supplier"]
    checkpoint = FakeGraph.last_config["external_request_checkpoint"]
    assert callable(supplier)
    assert 0 < FakeGraph.last_remaining_deadline <= run.run_timeout_seconds
    assert callable(checkpoint)


def test_runner_checkpoints_start_chunks_phases_and_external_boundaries(tmp_path, monkeypatch):
    manager = RunManager()
    run = manager.start_run(request(), run_id="run-heartbeat-checkpoints")
    manager.begin_run(run.run_id)
    calls = []
    original = manager.heartbeat

    def tracked(run_id):
        calls.append(run_id)
        return original(run_id)

    monkeypatch.setattr(manager, "heartbeat", tracked)
    WebRunRunner(
        manager,
        graph_factory=FakeGraph,
        config={"results_dir": str(tmp_path)},
    ).worker(run.run_id)
    assert calls.count(run.run_id) >= 4


class LateGraph(FakeGraph):
    def propagate(self, ticker, date, *, asset_type, on_chunk, should_cancel):
        self.config["deadline_supplier"]()
        return ({"market_report": "report", "final_trade_decision": "BUY"}, "BUY")


def test_runner_rejects_publication_after_deadline_expiry(tmp_path):
    manager = RunManager()
    run = manager.start_run(request(), run_id="run-late-worker")
    manager.begin_run(run.run_id)
    manager._records[run.run_id].record.timeout_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    WebRunRunner(
        manager,
        graph_factory=LateGraph,
        config={"results_dir": str(tmp_path)},
    ).worker(run.run_id)
    record = manager.get_run(run.run_id)
    assert record.status is RunStatus.TIMED_OUT
    assert not (tmp_path / "web_reports" / "AAPL" / "2026-08-26" / run.run_id).exists()


def test_runner_indexes_committed_report_before_terminal_completion(tmp_path, monkeypatch):
    manager = RunManager()
    run = manager.start_run(request(), run_id="run-index-order")
    manager.begin_run(run.run_id)
    history = TrackingHistory()
    original_complete = manager.complete_publishing

    def complete(*args, **kwargs):
        assert history.indexed
        assert (history.indexed[0] / "COMMITTED").is_file()
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(manager, "complete_publishing", complete)
    WebRunRunner(
        manager,
        graph_factory=FakeGraph,
        config={"results_dir": str(tmp_path)},
        report_history=history,
    ).worker(run.run_id)
    assert manager.get_run(run.run_id).status is RunStatus.COMPLETED
