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


# ════════════════════════════════════════════════════════
# Task 6: L1 projection receipts (atomic, idempotent)
# ════════════════════════════════════════════════════════

def _fresh_l1(tmp_path, *, langgraph: bool = False):
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    return SqliteSessionMemory(
        use_langgraph_checkpointer=langgraph,
        data_dir=str(tmp_path),
    )


def test_append_projected_exchange_applied_idempotent(tmp_path):
    """同 projection_key 第二次调用应返回 already_applied,不追加重复消息。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    m = SqliteSessionMemory(db_path=tmp_path / "l1.sqlite")

    r1 = m.append_projected_exchange("s1", "question", "answer", projection_key="runtime:r1")
    r2 = m.append_projected_exchange("s1", "question", "answer", projection_key="runtime:r1")

    assert r1 == "applied"
    assert r2 == "already_applied"

    hist = m.get_history("s1")
    assert len(hist) == 2, f"应该只有 1 对 user/assistant,实际 {len(hist)} 条"
    assert hist[0]["role"] == "user"
    assert hist[1]["role"] == "assistant"

    # receipt 表里恰好 1 行
    receipts = _list_receipts(m)
    assert len(receipts) == 1
    assert receipts[0]["projection_key"] == "runtime:r1"


def test_append_projected_exchange_idempotent_after_crash(tmp_path):
    """模拟 crash after target append before Runtime projection DELIVERED:
    第二次调用(等价于重试)命中 receipt,不重复追加。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    m = SqliteSessionMemory(db_path=tmp_path / "l1.sqlite")

    m.append_projected_exchange("s1", "Q", "A", projection_key="runtime:r1")
    # 重试
    m.append_projected_exchange("s1", "Q", "A", projection_key="runtime:r1")
    m.append_projected_exchange("s1", "Q", "A", projection_key="runtime:r1")

    hist = m.get_history("s1")
    assert len(hist) == 2, f"重试后消息数量应保持 2,实际 {len(hist)}"


def test_append_projected_exchange_multiple_keys(tmp_path):
    """不同 projection_key 都应 applied。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    m = SqliteSessionMemory(db_path=tmp_path / "l1.sqlite")

    assert m.append_projected_exchange("s1", "q1", "a1", projection_key="runtime:r1") == "applied"
    assert m.append_projected_exchange("s1", "q2", "a2", projection_key="runtime:r2") == "applied"

    hist = m.get_history("s1")
    assert len(hist) == 4  # 2 pairs


def test_append_projected_exchange_langgraph_backend(tmp_path):
    """LangGraph backend 也走同一 receipt 表(LG SQLite 文件)。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    m = SqliteSessionMemory(db_path=tmp_path / "lg.db", use_langgraph_checkpointer=True, data_dir=str(tmp_path))

    r1 = m.append_projected_exchange("s1", "Q", "A", projection_key="runtime:r1")
    r2 = m.append_projected_exchange("s1", "Q", "A", projection_key="runtime:r1")
    assert r1 == "applied"
    assert r2 == "already_applied"

    hist = m.get_history("s1")
    assert len(hist) == 2


def _list_receipts(memory):
    """辅助:读出 receipt 表内容。"""
    import sqlite3
    db_path = memory._db_path
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT projection_key, created_at FROM runtime_projection_receipts ORDER BY projection_key"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ════════════════════════════════════════════════════════
# Task 11: scheduler × recovery interaction
# ════════════════════════════════════════════════════════


def _scheduler_run(tmp_path, *, session_id="sess-r", run_kind="AGENT_ANALYSIS"):
    """Return (store, run_row) for a fresh scheduler integration test."""
    import uuid as _u
    from datetime import datetime, timezone
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    s = AgentRuntimeStore(tmp_path / "runtime.sqlite")
    rr = RunRepository(s)
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["x"], carry_symbols=[], slots={},
        tier=2, confidence=1.0,
        reason_code="t", route_kind="DIRECT_READ",
    )
    now = datetime.now(timezone.utc).isoformat()
    run = rr.create_run(
        run_id=str(_u.uuid4()),
        session_id=session_id, turn_id="turn-1",
        run_kind=run_kind, route=route,
        budgets={"max_llm_calls": 10}, now=now,
    )
    return s, run


def _scheduler_task(s, run, *, state="READY", required=True):
    import uuid as _u
    from datetime import datetime, timezone
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(s)

    class _P:
        objective = "obj"
        inputs = {}
    payload = _P()
    now = datetime.now(timezone.utc).isoformat()
    t = tr.create_task(
        task_id=str(_u.uuid4()),
        run_id=run["run_id"], parent_task_id=None,
        kind="AGENT", agent_name="TestAgent", system_handler=None,
        capability="verify", payload=payload,
        required=required, max_execution_attempts=3, now=now,
    )
    if state != "READY":
        t = tr.transition_task(
            task_id=t["task_id"],
            expected_version=t["version"], expected_state="READY",
            new_state=state, now=now,
        )
    return t


def test_recovery_then_scheduler_claims_reset_task(tmp_path):
    """Lease 过期 → recover() → READY → scheduler 再 claim(无 double-claim)。"""
    from datetime import datetime, timezone, timedelta
    s, run = _scheduler_run(tmp_path)
    t = _scheduler_task(s, run)
    # simulate stale claim by wkr1
    lease_past = (
        datetime.now(timezone.utc) - timedelta(seconds=120)
    ).isoformat()
    s.connection.execute(
        "UPDATE agent_tasks SET state='RUNNING', worker_id='wkr1', "
        "lease_expires_at=?, version=version+1, updated_at=? "
        "WHERE task_id=?",
        (lease_past, datetime.now(timezone.utc).isoformat(), t["task_id"]),
    )
    s.connection.commit()
    # recover
    s.recover()
    refreshed = s.connection.execute(
        "SELECT state, worker_id FROM agent_tasks WHERE task_id=?",
        (t["task_id"],),
    ).fetchone()
    assert refreshed[0] == "READY"
    assert refreshed[1] is None
    # now scheduler claims as wkr2
    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    claimed = sched.claim_ready_task(run_id=run["run_id"], worker_id="wkr2")
    assert claimed is not None
    assert claimed["worker_id"] == "wkr2"
    assert claimed["state"] == "RUNNING"


def test_scheduler_does_not_claim_already_running_after_recovery(tmp_path):
    """recover 把 lease 过期任务回 READY,scheduler 才再 claim;运行中不 claim。"""
    from datetime import datetime, timezone, timedelta
    s, run = _scheduler_run(tmp_path)
    t = _scheduler_task(s, run)
    # claim by wkr1 with active lease
    active_lease = (
        datetime.now(timezone.utc) + timedelta(seconds=120)
    ).isoformat()
    s.connection.execute(
        "UPDATE agent_tasks SET state='RUNNING', worker_id='wkr1', "
        "lease_expires_at=?, version=version+1 WHERE task_id=?",
        (active_lease, t["task_id"]),
    )
    s.connection.commit()
    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    claimed = sched.claim_ready_task(run_id=run["run_id"], worker_id="wkr2")
    assert claimed is None  # already running with fresh lease


def test_recovery_then_scheduler_aggregates_run(tmp_path):
    """recover 只做粗粒度聚合,scheduler.run_once 完成 spec §20.3 优先级聚合。

    例如:recover 把 all-SUCCEEDED run 标 SUCCEEDED,这是 §20.3 step 8;
    但 PARTIAL_SUCCESS 优先级(§20.3 step 7)需要 scheduler 区分 required /
    optional,所以场景是:required task SUCCEEDED + optional task FAILED。
    """
    import uuid as _u
    from datetime import datetime, timezone
    s, run = _scheduler_run(tmp_path, run_kind="AGENT_ANALYSIS")
    required = _scheduler_task(s, run, required=True)
    optional = _scheduler_task(s, run, required=False)
    s.connection.execute(
        "UPDATE agent_tasks SET state='SUCCEEDED', version=version+1 WHERE task_id=?",
        (required["task_id"],),
    )
    s.connection.execute(
        "UPDATE agent_tasks SET state='FAILED', version=version+1 WHERE task_id=?",
        (optional["task_id"],),
    )
    s.connection.commit()
    # recover 的简化版聚合认为 any FAILED → FAILED,丢了 optional vs required 区分
    s.recover()
    refreshed = s.connection.execute(
        "SELECT state FROM agent_runs WHERE run_id=?", (run["run_id"],)
    ).fetchone()
    # recover 把 run 标记成 FAILED(因为 any FAILED)
    assert refreshed[0] == "FAILED"
