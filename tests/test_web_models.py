from datetime import date

import pytest
from pydantic import ValidationError

from web.models import (
    ActivityPayload,
    AgentStatusPayload,
    AnalysisRequest,
    EventEnvelope,
    EventName,
    HistoryRecord,
    MessagePayload,
    PhaseChangedPayload,
    ProgressPayload,
    RunCancelledPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunRecord,
    RunSnapshotPayload,
    RunStartedPayload,
)


def test_request_normalizes_and_filters_crypto_fundamentals():
    req = AnalysisRequest(
        ticker=" btcusd ", analysis_date="2026-08-26", asset_type="crypto",
        analysts=["market", "fundamentals"], research_depth=3,
    )
    assert req.ticker == "BTC-USD"
    assert req.analysis_date == date(2026, 8, 26)
    assert req.analysts == ["market"]


@pytest.mark.parametrize("field,value", [("asset_type", "forex"), ("research_depth", 2), ("analysts", ["market", "market"])])
def test_invalid_values_are_rejected(field, value):
    data = {"ticker": "AAPL", "analysis_date": "2026-08-26", "analysts": ["market"], "research_depth": 1}
    data[field] = value
    with pytest.raises(ValidationError):
        AnalysisRequest(**data)


@pytest.mark.parametrize("ticker", ["AAPL/$", "AAPL x", "a" * 33])
def test_unsafe_ticker_is_rejected(ticker):
    with pytest.raises(ValidationError):
        AnalysisRequest(ticker=ticker, analysis_date="2026-08-26", analysts=["market"], research_depth=1)


@pytest.mark.parametrize("when", ["2026-02-30", "20260826", "2026-8-26"])
def test_date_must_be_strict_iso(when):
    with pytest.raises(ValidationError):
        AnalysisRequest(ticker="AAPL", analysis_date=when, analysts=["market"], research_depth=1)


def test_empty_effective_analysts_rejected_for_crypto():
    with pytest.raises(ValidationError):
        AnalysisRequest(ticker="BTC-USD", analysis_date="2026-08-26", asset_type="crypto", analysts=["fundamentals"], research_depth=1)


def test_typed_run_history_and_event_models():
    run = RunRecord(run_id="run-1", request=AnalysisRequest(ticker="AAPL", analysis_date="2026-08-26", analysts=["market"], research_depth=1))
    history = HistoryRecord(run_id=run.run_id, ticker="AAPL", analysis_date=date(2026, 8, 26), status="queued")
    event = EventEnvelope(
        run_id=run.run_id,
        seq=1,
        event="run_started",
        payload={
            "status": "running",
            "ticker": "AAPL",
            "analysis_date": "2026-08-26",
            "asset_type": "stock",
            "analysts": ["market"],
            "research_depth": 1,
        },
    )
    assert run.run_id == history.run_id == event.run_id
    assert event.seq == 1


def test_event_payloads_are_typed_and_validate_required_fields():
    event = EventEnvelope(
        run_id="run-1",
        seq=2,
        event=EventName.RUN_STARTED,
        payload={
            "status": "running",
            "ticker": "AAPL",
            "analysis_date": "2026-08-26",
            "asset_type": "stock",
            "analysts": ["market"],
            "research_depth": 1,
        },
    )
    assert isinstance(event.payload, RunStartedPayload)
    assert event.payload["status"] == "running"

    with pytest.raises(ValidationError):
        EventEnvelope(
            run_id="run-1",
            seq=3,
            event="run_started",
            payload={
                "status": "completed",
                "ticker": "AAPL",
                "analysis_date": "2026-08-26",
                "asset_type": "stock",
                "analysts": ["market"],
                "research_depth": 1,
            },
        )


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("phase_changed", {"phase": "research", "phase_index": 1, "phase_count": 4}),
        ("agent_status", {"agent": "market", "status": "unknown"}),
        ("progress", {"progress": 1.5, "phase": "research", "current_agent": None}),
        ("message", {"message_type": "log"}),
        ("activity", {"activity_type": "graph_update", "name": "node"}),
        ("run_completed", {"status": "completed", "signal": None}),
        ("run_failed", {"status": "failed", "error_code": "provider"}),
        ("run_cancelled", {"status": "cancelled", "phase": "research"}),
    ],
)
def test_event_payloads_reject_missing_or_invalid_required_fields(event, payload):
    with pytest.raises(ValidationError):
        EventEnvelope(run_id="run-1", seq=1, event=event, payload=payload)


def test_all_payload_models_are_constructible():
    request = AnalysisRequest(ticker="AAPL", analysis_date="2026-08-26", analysts=["market"], research_depth=1)
    run = RunRecord(run_id="run-1", request=request)
    assert RunSnapshotPayload(run=run, replay_from_seq=None)
    assert PhaseChangedPayload(phase="research", phase_index=1, phase_count=4, status="running")
    assert AgentStatusPayload(agent="market", status="pending")
    assert ProgressPayload(progress=0.5, phase="research", current_agent=None)
    assert MessagePayload(message_type="log", text="hello")
    assert ActivityPayload(activity_type="graph_update", name="node", summary="")
    assert RunCompletedPayload(status="completed", signal=None, report_id="report-1")
    assert RunFailedPayload(status="failed", error_code="provider", error_message="oops")
    assert RunCancelledPayload(status="cancelled", phase="research", current_agent=None)
