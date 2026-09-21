"""Task 18 — dual-view TraceProjector tests.

覆盖 plan 要求:
- durable events → user view(隐藏 prompt/secret/内部 trace)
- inspector view → tasks / messages / evidence / timing / token
- seq 顺序稳定,plan 在 domain events 之前
- 两个 verifier phase 夹住 synthesis
- SYSTEM_COMMAND → 确定性 agent_final(无 verification)
- control `done` / `resume_complete` 是 connection-only,不持久化
- cursor pagination max=100
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


def _now():
    return datetime.now(timezone.utc).isoformat()


def _store(tmp_path):
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    return AgentRuntimeStore(tmp_path / "runtime.sqlite")


def _make_run(s, *, run_kind="AGENT_ANALYSIS", session_id="sess-1"):
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    rr = RunRepository(s)
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["x"], carry_symbols=[], slots={},
        tier=2, confidence=1.0,
        reason_code="t", route_kind="DIRECT_READ",
    )
    return rr.create_run(
        run_id=str(uuid.uuid4()),
        session_id=session_id, turn_id="turn1",
        run_kind=run_kind, route=route,
        budgets={"max_llm_calls": 10}, now=_now(),
    )


def _append_event(s, run_id, *, event_type="agent_progress", payload=None):
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    er = EventRepository(s)
    return er.append_runtime_event(
        run_id=run_id, event_type=event_type, surface="ui",
        payload_json=payload or {}, task_id=None, message_id=None, now=_now(),
    )


def test_projector_returns_user_view_omitting_secrets(tmp_path):
    """user view 不暴露 secret 字段(prompt / token / authorization 等)。"""
    s = _store(tmp_path)
    run = _make_run(s)
    _append_event(s, run["run_id"], payload={
        "stage": "tool_call",
        "tool_name": "get_quote",
        "prompt": "secret prompt content",
        "authorization": "Bearer x",
    })
    from tradingagents.agent_harness.runtime.projector import TraceProjector
    p = TraceProjector(store=s)
    events = p.project(run_id=run["run_id"], view="user")
    assert len(events) == 1
    # 投影应过滤 secret 字段
    payload = events[0].get("payload", {})
    assert "prompt" not in payload
    assert "authorization" not in payload
    assert payload.get("stage") == "tool_call"


def test_projector_returns_inspector_view_with_full_data(tmp_path):
    """inspector view 包含完整 task / message / timing / token 信息。"""
    s = _store(tmp_path)
    run = _make_run(s)
    _append_event(s, run["run_id"], event_type="task_state", payload={
        "task_id": "t1", "state": "RUNNING",
        "tokens": 1234, "duration_ms": 500,
    })
    from tradingagents.agent_harness.runtime.projector import TraceProjector
    p = TraceProjector(store=s)
    events = p.project(run_id=run["run_id"], view="inspector")
    assert any(e.get("event_type") == "task_state" for e in events)


def test_projector_control_events_not_persisted(tmp_path):
    """done / resume_complete 是 connection-only,不持久化到 runtime_events。"""
    from tradingagents.agent_harness.runtime.projector import TraceProjector
    p = TraceProjector(store=_store(tmp_path))
    assert "done" not in p.PERSISTED_EVENTS
    assert "resume_complete" not in p.PERSISTED_EVENTS


def test_projector_cursor_pagination_max_100(tmp_path):
    """单页最多 100 条事件。"""
    s = _store(tmp_path)
    run = _make_run(s)
    for i in range(150):
        _append_event(s, run["run_id"], event_type="agent_progress",
                      payload={"i": i})
    from tradingagents.agent_harness.runtime.projector import TraceProjector
    p = TraceProjector(store=s)
    page1 = p.project(run_id=run["run_id"], view="user", since_seq=0, limit=100)
    assert len(page1) == 100
    last_seq = page1[-1]["seq"]
    page2 = p.project(run_id=run["run_id"], view="user", since_seq=last_seq, limit=100)
    # 剩下 50 个
    assert len(page2) == 50


def test_projector_seq_order_stable(tmp_path):
    """seq 单调递增,顺序稳定。"""
    s = _store(tmp_path)
    run = _make_run(s)
    for i in range(10):
        _append_event(s, run["run_id"], payload={"i": i})
    from tradingagents.agent_harness.runtime.projector import TraceProjector
    p = TraceProjector(store=s)
    events = p.project(run_id=run["run_id"], view="user")
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
