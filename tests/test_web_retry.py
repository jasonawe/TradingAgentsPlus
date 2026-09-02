"""Phase-2 retry from checkpoint: signature compatibility, retention, 409 downgrade."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, StateGraph

from tradingagents.graph.checkpointer import thread_id
from web.checkpoint_resume import (
    build_checkpoint_signature,
    checkpoint_available,
    clear_checkpoint_for_signature,
)
from web.error_codes import TerminalReason
from web.manager import RunManager
from web.models import AnalysisRequest, RunStatus
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


class _State(TypedDict):
    count: int


def _node_a(state):
    return {"count": state["count"] + 1}


def _node_b(state):
    return {"count": state["count"] + 10}


def _build_graph() -> StateGraph:
    builder = StateGraph(_State)
    builder.add_node("analyst", _node_a)
    builder.add_node("trader", _node_b)
    builder.set_entry_point("analyst")
    builder.add_edge("analyst", "trader")
    builder.add_edge("trader", END)
    return builder


def _create_checkpoint(data_dir: Path, ticker: str, date: str, signature: str) -> None:
    """Drop a LangGraph checkpoint for ``ticker/date`` matching ``signature``."""

    # ``checkpoint_available`` uses ``thread_id(ticker, date, signature)``
    # so we must use the same key here, otherwise the round-trip fails.
    tid = thread_id(ticker, date, signature)
    builder = _build_graph()
    from tradingagents.graph.checkpointer import get_checkpointer
    with get_checkpointer(data_dir, ticker) as saver:
        graph = builder.compile(checkpointer=saver)
        graph.invoke({"count": 1}, config={"configurable": {"thread_id": tid}})


def test_checkpoint_signature_changes_when_graph_inputs_change():
    base = build_checkpoint_signature(
        ticker="AAPL",
        analysis_date="2026-08-26",
        asset_type="stock",
        analysts=["market"],
        research_depth=1,
        output_language="English",
        provider="openai",
        quick_model="gpt-5.4-mini",
        deep_model="gpt-5.5",
    )
    # Same inputs are stable.
    assert base == build_checkpoint_signature(
        ticker="AAPL",
        analysis_date="2026-08-26",
        asset_type="stock",
        analysts=["market"],
        research_depth=1,
        output_language="English",
        provider="openai",
        quick_model="gpt-5.4-mini",
        deep_model="gpt-5.5",
    )
    # Each graph-affecting input changes the signature.
    assert base != build_checkpoint_signature(
        ticker="AAPL",
        analysis_date="2026-08-26",
        asset_type="stock",
        analysts=["market", "news"],
        research_depth=1,
        output_language="English",
        provider="openai",
        quick_model="gpt-5.4-mini",
        deep_model="gpt-5.5",
    )
    assert base != build_checkpoint_signature(
        ticker="AAPL",
        analysis_date="2026-08-26",
        asset_type="stock",
        analysts=["market"],
        research_depth=3,
        output_language="English",
        provider="openai",
        quick_model="gpt-5.4-mini",
        deep_model="gpt-5.5",
    )
    assert base != build_checkpoint_signature(
        ticker="AAPL",
        analysis_date="2026-08-26",
        asset_type="crypto",
        analysts=["market"],
        research_depth=1,
        output_language="English",
        provider="openai",
        quick_model="gpt-5.4-mini",
        deep_model="gpt-5.5",
    )


def test_can_retry_rejects_non_terminal_state(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    manager = RunManager(store=store)
    run = manager.start_run(_request(), run_id="queued")
    allowed, reason = manager.can_retry(run.run_id)
    assert allowed is False
    assert reason in {"run_not_retryable", "checkpoint_unavailable"}
    manager.shutdown()
    store.close()


def test_can_retry_rejects_when_reason_not_retryable(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    manager = RunManager(store=store)
    run = manager.start_run(_request(), run_id="auth-fail")
    manager.begin_run(run.run_id)
    # Auth errors are not retryable.
    manager.fail_run(
        run.run_id,
        error_code=TerminalReason.MODEL_AUTH_ERROR.value,
        error_message="bad key",
    )
    allowed, reason = manager.can_retry(run.run_id)
    assert allowed is False
    assert reason == "reason_not_retryable"
    manager.shutdown()
    store.close()


def test_can_retry_requires_existing_checkpoint(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    manager = RunManager(store=store)
    run = manager.start_run(_request(), run_id="no-ckpt")
    manager.begin_run(run.run_id)
    manager.fail_run(
        run.run_id,
        error_code=TerminalReason.MODEL_TIMEOUT.value,
        error_message="timed out",
        retryable=True,
    )
    # Manually clear resume_checkpoint_id so we look like a run without a
    # checkpoint.
    manager._state(run.run_id).record.resume_checkpoint_id = None
    manager._persist_locked(manager._state(run.run_id).record)
    allowed, reason = manager.can_retry(run.run_id)
    assert allowed is False
    assert reason == "checkpoint_unavailable"
    manager.shutdown()
    store.close()


def test_can_retry_rejects_when_another_run_active(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    manager = RunManager(store=store)
    parent = manager.start_run(_request(), run_id="parent")
    manager.begin_run(parent.run_id)
    manager.fail_run(
        parent.run_id,
        error_code=TerminalReason.MODEL_TIMEOUT.value,
        error_message="timed out",
        retryable=True,
    )
    # Pretend the run already retained a compatible checkpoint.
    manager._state(parent.run_id).record.resume_checkpoint_id = "sig-parent"
    manager._persist_locked(manager._state(parent.run_id).record)
    # Start a second, still-active run.
    second = manager.start_run(_request(ticker="MSFT"), run_id="other")
    manager.begin_run(second.run_id)
    allowed, reason = manager.can_retry(parent.run_id)
    assert allowed is False
    assert reason == "another_run_active"
    manager.cancel_run(second.run_id)
    manager.shutdown()
    store.close()


def test_retry_run_creates_a_new_run_with_parent_link(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    manager = RunManager(store=store)
    parent = manager.start_run(_request(), run_id="parent")
    manager.begin_run(parent.run_id)
    manager.fail_run(
        parent.run_id,
        error_code=TerminalReason.MODEL_TIMEOUT.value,
        error_message="timed out",
        retryable=True,
    )
    manager._state(parent.run_id).record.resume_checkpoint_id = "sig-1"
    manager._persist_locked(manager._state(parent.run_id).record)
    new_run = manager.retry_run(
        parent_run_id="parent",
        request=_request(),
        worker=lambda _run_id: None,
    )
    assert new_run.parent_run_id == "parent"
    assert new_run.attempt_number == 2
    assert new_run.resume_checkpoint_id == "sig-1"
    # The parent's terminal record is preserved as-is.
    parent_record = manager.get_run("parent")
    assert parent_record.status is RunStatus.FAILED
    manager.shutdown()
    store.close()


def test_retry_run_409_when_ineligible(tmp_path):
    store = SQLiteStore(tmp_path / "db.sqlite3")
    manager = RunManager(store=store)
    run = manager.start_run(_request(), run_id="still-running")
    manager.begin_run(run.run_id)
    with pytest.raises(RuntimeError):
        manager.retry_run(parent_run_id="still-running", request=_request())
    manager.shutdown()
    store.close()


def test_checkpoint_round_trip_via_signature_helper(tmp_path):
    """A checkpoint created with one signature is invisible to another."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sig_a = "analysts=market|asset=stock"
    sig_b = "analysts=market,news|asset=stock"
    _create_checkpoint(data_dir, "AAPL", "2026-08-26", sig_a)
    assert checkpoint_available(data_dir, "AAPL", "2026-08-26", sig_a)
    assert not checkpoint_available(data_dir, "AAPL", "2026-08-26", sig_b)
    clear_checkpoint_for_signature(data_dir, "AAPL", "2026-08-26", sig_a)
    assert not checkpoint_available(data_dir, "AAPL", "2026-08-26", sig_a)
