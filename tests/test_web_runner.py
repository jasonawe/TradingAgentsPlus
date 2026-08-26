import json
from pathlib import Path

from web.manager import RunManager
from web.models import AnalysisRequest, EventName, RunStatus
from web.runner import WebRunRunner


def request():
    return AnalysisRequest(ticker="AAPL", analysis_date="2026-08-26", asset_type="stock", analysts=["market"], research_depth=1)


class FakeGraph:
    def __init__(self, analysts, *, config, debug):
        self.config = config
        self.debug = debug

    def propagate(self, ticker, date, *, asset_type, on_chunk, should_cancel):
        on_chunk({"market_report": "report"})
        return ({"market_report": "report", "final_trade_decision": "BUY"}, "BUY")

    def save_reports(self, state, ticker, *, save_path):
        path = Path(save_path)
        path.mkdir(parents=True)
        (path / "complete_report.md").write_text("report", encoding="utf-8")


def test_runner_streams_events_and_writes_sidecar(tmp_path):
    manager = RunManager()
    run = manager.start_run(request(), run_id="run-1")
    manager.begin_run(run.run_id)
    WebRunRunner(manager, graph_factory=FakeGraph, config={"results_dir": str(tmp_path)}).worker(run.run_id)
    assert manager.get_run(run.run_id).status is RunStatus.COMPLETED
    events = manager.read_events(run.run_id).events
    assert EventName.AGENT_STATUS in [event.event for event in events]
    sidecar = tmp_path / "web_reports" / "AAPL" / "2026-08-26" / "run-1" / "run.json"
    assert json.loads(sidecar.read_text()) == {"run_id": "run-1", "report_id": "run-1"}


def test_runner_redacts_unknown_activity():
    manager = RunManager()
    run = manager.start_run(request(), run_id="run-2")
    manager.begin_run(run.run_id)
    runner = WebRunRunner(manager)
    runner._publish_chunk(run.run_id, {"unexpected": "api_key=secret"}, ["market"], {"name": "Analyst Team", "index": 1})
    event = manager.read_events(run.run_id).events[-1]
    assert event.event is EventName.ACTIVITY
    assert "secret" not in event.payload.summary

