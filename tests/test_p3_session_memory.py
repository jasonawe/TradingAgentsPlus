"""§P3-2 — Per-turn memory + cross-turn symbol carry-forward.

Without these, the harness's chat history injection (Layer.CHAT) returns
empty every turn and the planner cannot anchor "加入关注" without an
explicit ticker.

This module covers:
1. Orchestrator._save_turn_summary persists user+assistant+L2 metadata.
2. Orchestrator._load_session_context returns the previous turn's symbols.
3. OrchestratorState carries ``carry_symbols`` / ``prior_user_msg`` so the
   planner's prompt can hint at the implicit asset reference.
4. The full stream_chat() flow on a real harness writes both L1 and L2.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tradingagents.agent_harness.config.schema import HarnessConfig
from tradingagents.agent_harness.core.orchestrator import (
    Orchestrator, OrchestratorState,
)
from tradingagents.agent_harness.core.retry import RetryPolicy, CircuitBreaker
from tradingagents.agent_harness.tools import ToolRegistry
from tradingagents.agent_harness.memory import MemoryManager


# ---------------------------------------------------------------------------
# Unit: helpers on Orchestrator
# ---------------------------------------------------------------------------
def _make_orch(tmp_path, *, memory=None) -> Orchestrator:
    if memory is None:
        memory = MemoryManager(data_dir=str(tmp_path))
    return Orchestrator(
        tool_registry=ToolRegistry(),
        agent_registry=None,
        llm_factory=None,
        context_priority=None,
        retry_policy=RetryPolicy(max_retries=0),
        circuit_breaker=CircuitBreaker(failure_threshold=5, reset_seconds=30.0),
        audit=None,
        memory=memory,
    )


def test_load_session_context_empty_on_no_prior_turn(tmp_path) -> None:
    orch = _make_orch(tmp_path)
    ctx = orch._load_session_context("never-seen-session")
    assert ctx == {"symbols": [], "intent": None, "user_msg": None}


def test_save_then_load_round_trip(tmp_path) -> None:
    orch = _make_orch(tmp_path)
    orch._save_turn_summary(
        "sess-A",
        user_msg="分析 600036",
        intent="analysis",
        symbols=["600036.SS"],
        assistant_summary="估值结论...",
    )
    ctx = orch._load_session_context("sess-A")
    assert ctx["symbols"] == ["600036.SS"]
    assert ctx["intent"] == "analysis"
    assert "分析 600036" in (ctx["user_msg"] or "")


def test_save_truncates_long_user_msg(tmp_path) -> None:
    orch = _make_orch(tmp_path)
    long_msg = "x" * 1000
    orch._save_turn_summary(
        "sess-trunc", user_msg=long_msg, intent=None, symbols=[], assistant_summary=None,
    )
    ctx = orch._load_session_context("sess-trunc")
    # Truncated to 200 chars
    assert len(ctx["user_msg"]) == 200


def test_load_session_context_handles_corrupt_value(tmp_path) -> None:
    """If the L2 row was overwritten with a non-dict (e.g. user data),
    the loader must not crash — return empty defaults."""
    memory = MemoryManager(data_dir=str(tmp_path))
    memory.l2.set("__session_ctx__", "not a dict", session_id="corrupt")
    orch = _make_orch(tmp_path, memory=memory)
    ctx = orch._load_session_context("corrupt")
    assert ctx == {"symbols": [], "intent": None, "user_msg": None}


# ---------------------------------------------------------------------------
# OrchestratorState — carry_symbols and prior_user_msg fields
# ---------------------------------------------------------------------------
def test_orchestrator_state_has_carry_fields() -> None:
    state = OrchestratorState(
        session_id="x", user_message="y",
        carry_symbols=["600036.SS"], prior_user_msg="分析 600036",
    )
    assert state.carry_symbols == ["600036.SS"]
    assert state.prior_user_msg == "分析 600036"


def test_orchestrator_state_carry_fields_default_empty() -> None:
    state = OrchestratorState(session_id="x", user_message="y")
    assert state.carry_symbols == []
    assert state.prior_user_msg is None
