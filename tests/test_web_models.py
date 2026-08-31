from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from web.config import resolve_run_lifecycle_config
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
    RunStatus,
    RunTimedOutPayload,
)


def test_request_normalizes_and_filters_crypto_fundamentals():
    req = AnalysisRequest(
        ticker=" btcusd ", analysis_date="2026-08-26", asset_type="crypto",
        analysts=["market", "fundamentals"], research_depth=3,
    )
    assert req.ticker == "BTC-USD"
    assert req.analysis_date == date(2026, 8, 26)
    assert req.analysts == ["market"]


def test_request_accepts_web_output_language_override():
    req = AnalysisRequest(
        ticker="AAPL", analysis_date="2026-08-26", analysts=["market"],
        research_depth=1, output_language="Chinese",
    )
    assert req.output_language == "Chinese"


@pytest.mark.parametrize(
    "field",
    [
        "run_timeout_seconds",
        "run_heartbeat_interval_seconds",
        "run_heartbeat_timeout_seconds",
    ],
)
def test_request_rejects_server_owned_lifecycle_fields(field):
    data = {
        "ticker": "AAPL",
        "analysis_date": "2026-08-26",
        "analysts": ["market"],
        "research_depth": 1,
        field: 300,
    }
    with pytest.raises(ValidationError):
        AnalysisRequest(**data)


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
    assert event.model_dump(mode="json")["payload"]["status"] == "running"


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


def test_timed_out_run_and_payload_serialize_terminal_reason_alias():
    request = AnalysisRequest(
        ticker="AAPL",
        analysis_date="2026-08-26",
        analysts=["market"],
        research_depth=1,
    )
    run = RunRecord(
        run_id="run-timeout",
        request=request,
        status=RunStatus.TIMED_OUT,
        last_heartbeat_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        timeout_at=datetime(2026, 8, 26, 2, tzinfo=timezone.utc),
        terminal_reason="heartbeat_timeout",
        run_timeout_seconds=7200,
        run_heartbeat_interval_seconds=15,
        run_heartbeat_timeout_seconds=180,
    )
    dumped = run.model_dump(mode="json")
    assert dumped["terminal_reason"] == dumped["error_code"] == "heartbeat_timeout"
    assert dumped["run_timeout_seconds"] == 7200
    event = EventEnvelope(
        run_id=run.run_id,
        seq=1,
        event=EventName.RUN_TIMED_OUT,
        payload={
            "status": "timed_out",
            "progress": 0.45,
            "terminal_reason": "heartbeat_timeout",
            "error_message": "analysis heartbeat expired",
        },
    )
    assert isinstance(event.payload, RunTimedOutPayload)
    assert event.model_dump(mode="json")["payload"]["error_code"] == "heartbeat_timeout"


def test_run_lifecycle_config_defaults_and_precedence(monkeypatch):
    for key in (
        "TRADINGAGENTS_RUN_TIMEOUT_SECONDS",
        "TRADINGAGENTS_RUN_HEARTBEAT_INTERVAL_SECONDS",
        "TRADINGAGENTS_RUN_HEARTBEAT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    resolved = resolve_run_lifecycle_config({}, {})
    assert {key: item["value"] for key, item in resolved.items()} == {
        "run_timeout_seconds": 7200,
        "run_heartbeat_interval_seconds": 15,
        "run_heartbeat_timeout_seconds": 180,
    }
    assert {item["source"] for item in resolved.values()} == {"default_config"}

    settings = {
        "run_timeout_seconds": {"value": "3600", "source": "sqlite"},
        "run_heartbeat_interval_seconds": {"value": "20", "source": "sqlite"},
    }
    resolved = resolve_run_lifecycle_config(
        {
            "run_timeout_seconds": 5400,
            "run_heartbeat_interval_seconds": 30,
            "run_heartbeat_timeout_seconds": 240,
        },
        settings,
    )
    assert resolved["run_timeout_seconds"] == {"value": 3600, "source": "sqlite"}
    assert resolved["run_heartbeat_interval_seconds"] == {"value": 20, "source": "sqlite"}
    assert resolved["run_heartbeat_timeout_seconds"] == {"value": 240, "source": "config"}

    monkeypatch.setenv("TRADINGAGENTS_RUN_TIMEOUT_SECONDS", "1800")
    assert resolve_run_lifecycle_config({}, settings)["run_timeout_seconds"] == {
        "value": 1800,
        "source": "env",
    }


def test_run_lifecycle_environment_names_are_registered():
    from tradingagents.default_config import _ENV_OVERRIDES

    assert {
        "TRADINGAGENTS_RUN_TIMEOUT_SECONDS": "run_timeout_seconds",
        "TRADINGAGENTS_RUN_HEARTBEAT_INTERVAL_SECONDS": "run_heartbeat_interval_seconds",
        "TRADINGAGENTS_RUN_HEARTBEAT_TIMEOUT_SECONDS": "run_heartbeat_timeout_seconds",
    }.items() <= _ENV_OVERRIDES.items()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("run_timeout_seconds", 299),
        ("run_timeout_seconds", 86401),
        ("run_heartbeat_interval_seconds", 4),
        ("run_heartbeat_interval_seconds", 61),
        ("run_heartbeat_timeout_seconds", 29),
        ("run_heartbeat_timeout_seconds", 601),
        ("run_timeout_seconds", "not-an-int"),
        ("run_timeout_seconds", 86400.9),
        ("run_heartbeat_interval_seconds", "15.5"),
    ],
)
def test_run_lifecycle_config_rejects_invalid_values(monkeypatch, key, value):
    monkeypatch.delenv("TRADINGAGENTS_RUN_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_RUN_HEARTBEAT_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_RUN_HEARTBEAT_TIMEOUT_SECONDS", raising=False)
    with pytest.raises(ValueError, match=key):
        resolve_run_lifecycle_config({key: value}, {})


def test_run_lifecycle_config_uses_hard_fallback_when_defaults_are_unavailable(
    monkeypatch,
):
    for env_key in (
        "TRADINGAGENTS_RUN_TIMEOUT_SECONDS",
        "TRADINGAGENTS_RUN_HEARTBEAT_INTERVAL_SECONDS",
        "TRADINGAGENTS_RUN_HEARTBEAT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(env_key, raising=False)
    for key in (
        "run_timeout_seconds",
        "run_heartbeat_interval_seconds",
        "run_heartbeat_timeout_seconds",
    ):
        monkeypatch.delitem(__import__("web.config", fromlist=["DEFAULT_CONFIG"]).DEFAULT_CONFIG, key)
    resolved = resolve_run_lifecycle_config({}, {})
    assert {key: item["value"] for key, item in resolved.items()} == {
        "run_timeout_seconds": 7200,
        "run_heartbeat_interval_seconds": 15,
        "run_heartbeat_timeout_seconds": 180,
    }
    assert {item["source"] for item in resolved.values()} == {"hard_fallback"}
