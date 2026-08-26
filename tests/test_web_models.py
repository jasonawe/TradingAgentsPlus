from datetime import date

import pytest
from pydantic import ValidationError

from web.models import AnalysisRequest, EventEnvelope, HistoryRecord, RunRecord


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
    event = EventEnvelope(run_id=run.run_id, seq=1, event="run_started", payload={"status": "running"})
    assert run.run_id == history.run_id == event.run_id
    assert event.seq == 1
