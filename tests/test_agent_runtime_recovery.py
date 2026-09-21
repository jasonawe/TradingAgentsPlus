"""Task 5 — runtime recovery scan tests.

覆盖 plan 要求:
- crash after business commit before enqueue → recovery enqueues scheduler row
- crash after child completion before wait resolution → wait resolves
- crash after task completion before run aggregation → run aggregates
- claimed-outbox timeout → reset to PENDING

recovery 调用方契约:
- `AgentRuntimeStore.recover(now=None)` 一次性扫描并修复所有异常状态
- 在同一事务内完成 lease 过期重置 + dependency readiness 重算 + wait resolution + run aggregation
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


def _store(tmp_path):
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    return AgentRuntimeStore(tmp_path / "runtime.sqlite")


def _make_run(store, session_id="sess-1"):
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    rr = RunRepository(store)
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["x"], carry_symbols=[], slots={},
        tier=2, confidence=1.0,
        reason_code="t", route_kind="DIRECT_READ",
    )
    now = datetime.now(timezone.utc).isoformat()
    return rr.create_run(
        run_id=str(uuid.uuid4()),
        session_id=session_id, turn_id="turn-1",
        run_kind="AGENT_ANALYSIS", route=route,
        budgets={"max_llm_calls": 10}, now=now,
    )


def _make_task(store, run, *, kind="PLANNER", state="READY"):
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(store)
    class _FP:
        objective = "obj"
        inputs = {}
    payload = _FP()
    now = datetime.now(timezone.utc).isoformat()
    t = tr.create_task(
        task_id=str(uuid.uuid4()),
        run_id=run["run_id"], parent_task_id=None,
        kind=kind, agent_name=None, system_handler=None,
        capability="planning", payload=payload,
        required=True, max_execution_attempts=3, now=now,
    )
    if state != "READY":
        t = tr.transition_task(
            task_id=t["task_id"],
            expected_version=t["version"], expected_state="READY",
            new_state=state, now=now,
        )
    return t


def _make_wait(store, run, parent_task, child_task):
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(store)
    now = datetime.now(timezone.utc).isoformat()
    return tr.create_wait(
        run_id=run["run_id"],
        waiter_task_id=parent_task["task_id"],
        child_task_id=child_task["task_id"],
        wait_kind="CHILD_TASK", failure_policy="FAIL_RUN", now=now,
    )


# ════════════════════════════════════════════════════════
# claimed-outbox timeout → reset to PENDING
# ════════════════════════════════════════════════════════

def test_recovery_resets_expired_claimed_outbox(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    ev_row = _append_event(s, r)
    _enqueue_outbox(s, r, ev_row, delivery_key="sse|k1")

    # claim it
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    er = EventRepository(s)
    rows = er.list_outbox(r["run_id"])
    assert len(rows) == 1
    s.connection.execute(
        "UPDATE agent_outbox SET state='CLAIMED', claimed_at=?, claimed_by='wkr' WHERE outbox_id=?",
        ((datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(), rows[0]["outbox_id"]),
    )
    s.connection.commit()

    s.recover()
    row = er.list_outbox(r["run_id"])[0]
    assert row["state"] == "PENDING"
    assert row["claimed_at"] is None


# ════════════════════════════════════════════════════════
# expired lease RUNNING task → READY
# ════════════════════════════════════════════════════════

def test_recovery_resets_expired_task_lease(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, state="RUNNING")
    # mark lease expired
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    s.connection.execute(
        "UPDATE agent_tasks SET lease_expires_at=?, worker_id='wkr' WHERE task_id=?",
        (old, t["task_id"]),
    )
    s.connection.commit()

    s.recover()
    row = s.connection.execute(
        "SELECT state, version FROM agent_tasks WHERE task_id=?", (t["task_id"],)
    ).fetchone()
    assert row[0] == "READY"
    assert row[1] >= t["version"] + 1


# ════════════════════════════════════════════════════════
# wait resolution after child SUCCEEDED
# ════════════════════════════════════════════════════════

def test_recovery_resolves_persisted_waits(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    parent = _make_task(s, r, state="WAITING_CHILD")
    child = _make_task(s, r, state="SUCCEEDED")
    wait = _make_wait(s, r, parent, child)
    assert wait["state"] == "WAITING"

    s.recover()
    row = s.connection.execute(
        "SELECT state, resolved_at FROM agent_task_waits WHERE wait_id=?", (wait["wait_id"],)
    ).fetchone()
    assert row[0] == "RESOLVED"
    assert row[1] is not None


# ════════════════════════════════════════════════════════
# run aggregation: all tasks SUCCEEDED → run aggregates
# ════════════════════════════════════════════════════════

def test_recovery_aggregates_run_when_all_tasks_terminal(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    _make_task(s, r, state="SUCCEEDED")
    _make_task(s, r, state="SUCCEEDED")
    # run is still PLANNING because crash before aggregation
    assert r["state"] == "PLANNING"

    s.recover()
    after = s.connection.execute(
        "SELECT state FROM agent_runs WHERE run_id=?", (r["run_id"],)
    ).fetchone()
    assert after[0] in ("SUCCEEDED", "PARTIAL_SUCCESS", "FAILED"), (
        f"run should be aggregated, got {after[0]}"
    )


def _append_event(store, run):
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    er = EventRepository(store)
    return er.append_runtime_event(
        run_id=run["run_id"], event_type="PROGRESS",
        surface="PUBLIC", payload_json={"k": "v"},
        now=datetime.now(timezone.utc).isoformat(),
    )


def _enqueue_outbox(store, run, ev, *, destination="sse", delivery_key=None):
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    er = EventRepository(store)
    er.append_outbox(
        run_id=run["run_id"], task_id=None, message_id=None,
        source_event_id=ev["event_id"],
        delivery_key=delivery_key or f"{destination}|{ev['event_id']}",
        destination=destination,
        payload_json={"event_id": ev["event_id"]},
        now=datetime.now(timezone.utc).isoformat(),
    )
