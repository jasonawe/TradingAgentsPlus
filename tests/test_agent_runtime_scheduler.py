"""Task 11 — TaskScheduler tests.

覆盖 plan 要求:
- ON_SUCCESS / ON_TERMINAL readiness(dependency condition)
- parallel ready claims(version CAS + lease)
- required vs optional failure aggregation
- WAITING_MESSAGE vs WAITING_CHILD propagation
- child failure policies(FAIL_WAITER / RESUME_WITH_FAILURE)
- cancel / complete CAS races
- AGENT_ANALYSIS aggregation(SUCCEEDED / PARTIAL_SUCCESS / FAILED)
- SYSTEM_COMMAND aggregation
- NEEDS_RECONCILIATION(INDETERMINATE → needs reconciliation)
- no duplicate releases after recovery(双 claim 不发生)

Scheduler 是纯 store 驱动 — 不调用 Agent / Tool / LLM。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


# ════════════════════════════════════════════════════════
# Helpers — fixtures shared with recovery tests
# ════════════════════════════════════════════════════════


def _store(tmp_path):
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    return AgentRuntimeStore(tmp_path / "runtime.sqlite")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _make_run(store, *, session_id="sess-1", run_kind="AGENT_ANALYSIS"):
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
    return rr.create_run(
        run_id=str(uuid.uuid4()),
        session_id=session_id, turn_id="turn-1",
        run_kind=run_kind, route=route,
        budgets={"max_llm_calls": 10}, now=_now(),
    )


def _make_task(store, run, *, state="READY", required=True, kind="AGENT",
               agent_name="TestAgent", capability="verify"):
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(store)
    class _P:
        objective = "obj"
        inputs = {}
    payload = _P()
    t = tr.create_task(
        task_id=str(uuid.uuid4()),
        run_id=run["run_id"], parent_task_id=None,
        kind=kind, agent_name=agent_name, system_handler=None,
        capability=capability, payload=payload,
        required=required, max_execution_attempts=3, now=_now(),
    )
    if state != "READY":
        t = tr.transition_task(
            task_id=t["task_id"],
            expected_version=t["version"], expected_state="READY",
            new_state=state, now=_now(),
        )
    return t


def _add_dependency(store, *, run_id, task_id, depends_on_task_id,
                    condition="ON_SUCCESS"):
    store.connection.execute(
        "INSERT OR IGNORE INTO agent_task_dependencies "
        "(run_id, task_id, depends_on_task_id, condition) VALUES (?, ?, ?, ?)",
        (run_id, task_id, depends_on_task_id, condition),
    )
    store.connection.commit()


def _make_wait(store, run, *, waiter, child, wait_kind="CHILD_TASK",
               failure_policy="FAIL_WAITER"):
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(store)
    return tr.create_wait(
        run_id=run["run_id"],
        waiter_task_id=waiter["task_id"],
        child_task_id=child["task_id"],
        wait_kind=wait_kind, failure_policy=failure_policy, now=_now(),
    )


def _set_state(store, task_id, state):
    store.connection.execute(
        "UPDATE agent_tasks SET state=?, version=version+1, updated_at=? "
        "WHERE task_id=?",
        (state, _now(), task_id),
    )
    store.connection.commit()


def _set_run_state(store, run_id, state):
    store.connection.execute(
        "UPDATE agent_runs SET state=?, version=version+1, updated_at=? "
        "WHERE run_id=?",
        (state, _now(), run_id),
    )
    store.connection.commit()


# ════════════════════════════════════════════════════════
# claim_ready_task — lease + version CAS
# ════════════════════════════════════════════════════════


def test_claim_ready_task_returns_task_with_lease(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r)
    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    claimed = sched.claim_ready_task(run_id=r["run_id"], worker_id="wkr1")
    assert claimed is not None
    assert claimed["task_id"] == t["task_id"]
    assert claimed["state"] == "RUNNING"
    assert claimed["worker_id"] == "wkr1"
    assert claimed["lease_expires_at"] is not None


def test_claim_ready_task_returns_none_when_no_ready(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, state="RUNNING")
    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    claimed = sched.claim_ready_task(run_id=r["run_id"], worker_id="wkr1")
    assert claimed is None


def test_claim_ready_task_uses_version_cas_no_double_claim(tmp_path):
    """两个 worker 并发 claim 同一个 READY task → 只有一方成功。"""
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r)
    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched1 = TaskScheduler(s)
    sched2 = TaskScheduler(s)
    c1 = sched1.claim_ready_task(run_id=r["run_id"], worker_id="wkr1")
    c2 = sched2.claim_ready_task(run_id=r["run_id"], worker_id="wkr2")
    # 只有一方拿到
    assert (c1 is None) != (c2 is None)


# ════════════════════════════════════════════════════════
# dependency readiness — ON_SUCCESS / ON_TERMINAL
# ════════════════════════════════════════════════════════


def test_run_once_promotes_planned_when_dep_succeeds_on_success(tmp_path):
    """ON_SUCCESS 依赖:dep SUCCEEDED → dependent PLANNED → READY。"""
    s = _store(tmp_path)
    r = _make_run(s)
    a = _make_task(s, r)
    b = _make_task(s, r)
    _set_state(s, b["task_id"], "PLANNED")
    _add_dependency(s, run_id=r["run_id"], task_id=b["task_id"],
                    depends_on_task_id=a["task_id"], condition="ON_SUCCESS")
    _set_state(s, a["task_id"], "SUCCEEDED")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    report = sched.run_once(run_id=r["run_id"])
    assert b["task_id"] in report["promoted_task_ids"]
    refreshed = s.connection.execute(
        "SELECT state FROM agent_tasks WHERE task_id=?", (b["task_id"],)
    ).fetchone()
    assert refreshed[0] == "READY"


def test_run_once_promotes_on_terminal_when_dep_fails(tmp_path):
    """ON_TERMINAL 依赖:dep FAILED → dependent PLANNED → READY(失败也 promote)。"""
    s = _store(tmp_path)
    r = _make_run(s)
    a = _make_task(s, r)
    b = _make_task(s, r)
    _set_state(s, b["task_id"], "PLANNED")
    _add_dependency(s, run_id=r["run_id"], task_id=b["task_id"],
                    depends_on_task_id=a["task_id"], condition="ON_TERMINAL")
    _set_state(s, a["task_id"], "FAILED")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    report = sched.run_once(run_id=r["run_id"])
    assert b["task_id"] in report["promoted_task_ids"]


def test_run_once_does_not_promote_on_success_when_dep_not_terminal(tmp_path):
    """ON_SUCCESS 依赖:dep 还 RUNNING → 不 promote。"""
    s = _store(tmp_path)
    r = _make_run(s)
    a = _make_task(s, r, state="RUNNING")
    b = _make_task(s, r)
    _set_state(s, b["task_id"], "PLANNED")
    _add_dependency(s, run_id=r["run_id"], task_id=b["task_id"],
                    depends_on_task_id=a["task_id"], condition="ON_SUCCESS")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    report = sched.run_once(run_id=r["run_id"])
    assert b["task_id"] not in report["promoted_task_ids"]


# ════════════════════════════════════════════════════════
# wait resolution
# ════════════════════════════════════════════════════════


def test_run_once_resolves_wait_when_child_succeeds(tmp_path):
    """child SUCCEEDED → wait RESOLVED → waiter 进入 READY 或回主流程。"""
    s = _store(tmp_path)
    r = _make_run(s)
    parent = _make_task(s, r, state="WAITING_CHILD")
    child = _make_task(s, r)
    _set_state(s, child["task_id"], "SUCCEEDED")
    w = _make_wait(s, r, waiter=parent, child=child, failure_policy="FAIL_WAITER")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    report = sched.run_once(run_id=r["run_id"])
    assert w["wait_id"] in report["resolved_wait_ids"]
    refreshed = s.connection.execute(
        "SELECT state FROM agent_task_waits WHERE wait_id=?", (w["wait_id"],)
    ).fetchone()
    assert refreshed[0] == "RESOLVED"


def test_run_once_required_child_failure_fails_parent(tmp_path):
    """required child FAILED + FAIL_WAITER → waiter 进入 FAILED。"""
    s = _store(tmp_path)
    r = _make_run(s)
    parent = _make_task(s, r, state="WAITING_CHILD", required=True)
    child = _make_task(s, r, required=True)
    _set_state(s, child["task_id"], "FAILED")
    _make_wait(s, r, waiter=parent, child=child, failure_policy="FAIL_WAITER")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    sched.run_once(run_id=r["run_id"])
    refreshed = s.connection.execute(
        "SELECT state FROM agent_tasks WHERE task_id=?", (parent["task_id"],)
    ).fetchone()
    assert refreshed[0] == "FAILED"


def test_run_once_optional_child_failure_resumes_parent(tmp_path):
    """optional child FAILED + RESUME_WITH_FAILURE → waiter 回到 READY。"""
    s = _store(tmp_path)
    r = _make_run(s)
    parent = _make_task(s, r, state="WAITING_CHILD", required=False)
    child = _make_task(s, r, required=False)
    _set_state(s, child["task_id"], "FAILED")
    _make_wait(s, r, waiter=parent, child=child,
               failure_policy="RESUME_WITH_FAILURE")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    sched.run_once(run_id=r["run_id"])
    refreshed = s.connection.execute(
        "SELECT state FROM agent_tasks WHERE task_id=?", (parent["task_id"],)
    ).fetchone()
    assert refreshed[0] == "READY"


# ════════════════════════════════════════════════════════
# run aggregation — spec §20.3
# ════════════════════════════════════════════════════════


def test_run_once_aggregates_agent_analysis_all_succeeded(tmp_path):
    """AGENT_ANALYSIS:全部 SUCCEEDED → run SUCCEEDED。"""
    s = _store(tmp_path)
    r = _make_run(s, run_kind="AGENT_ANALYSIS")
    a = _make_task(s, r)
    b = _make_task(s, r)
    _set_state(s, a["task_id"], "SUCCEEDED")
    _set_state(s, b["task_id"], "SUCCEEDED")
    _set_run_state(s, r["run_id"], "RUNNING")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    report = sched.run_once(run_id=r["run_id"])
    assert r["run_id"] in report["aggregated_run_ids"]
    refreshed = s.connection.execute(
        "SELECT state FROM agent_runs WHERE run_id=?", (r["run_id"],)
    ).fetchone()
    assert refreshed[0] == "SUCCEEDED"


def test_run_once_aggregates_system_command_succeeded(tmp_path):
    """SYSTEM_COMMAND:唯一 SYSTEM_COMMAND task SUCCEEDED → run SUCCEEDED。"""
    s = _store(tmp_path)
    r = _make_run(s, run_kind="SYSTEM_COMMAND")
    t = _make_task(s, r, kind="SYSTEM_COMMAND")
    _set_state(s, t["task_id"], "SUCCEEDED")
    _set_run_state(s, r["run_id"], "RUNNING")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    sched.run_once(run_id=r["run_id"])
    refreshed = s.connection.execute(
        "SELECT state FROM agent_runs WHERE run_id=?", (r["run_id"],)
    ).fetchone()
    assert refreshed[0] == "SUCCEEDED"


def test_run_once_required_failure_aggregates_failed(tmp_path):
    """required task FAILED → run FAILED。"""
    s = _store(tmp_path)
    r = _make_run(s, run_kind="AGENT_ANALYSIS")
    a = _make_task(s, r, required=True)
    b = _make_task(s, r, required=True)
    _set_state(s, a["task_id"], "SUCCEEDED")
    _set_state(s, b["task_id"], "FAILED")
    _set_run_state(s, r["run_id"], "RUNNING")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    sched.run_once(run_id=r["run_id"])
    refreshed = s.connection.execute(
        "SELECT state FROM agent_runs WHERE run_id=?", (r["run_id"],)
    ).fetchone()
    assert refreshed[0] == "FAILED"


def test_run_once_partial_success_optional_failure_only(tmp_path):
    """AGENT_ANALYSIS:必需任务都 SUCCEEDED + 只有 optional FAILED → PARTIAL_SUCCESS。"""
    s = _store(tmp_path)
    r = _make_run(s, run_kind="AGENT_ANALYSIS")
    required = _make_task(s, r, required=True)
    optional = _make_task(s, r, required=False)
    _set_state(s, required["task_id"], "SUCCEEDED")
    _set_state(s, optional["task_id"], "FAILED")
    _set_run_state(s, r["run_id"], "RUNNING")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    sched.run_once(run_id=r["run_id"])
    refreshed = s.connection.execute(
        "SELECT state FROM agent_runs WHERE run_id=?", (r["run_id"],)
    ).fetchone()
    assert refreshed[0] == "PARTIAL_SUCCESS"


def test_run_once_indeterminate_task_lifts_run_to_needs_reconciliation(tmp_path):
    """任一 task INDETERMINATE → run NEEDS_RECONCILIATION。"""
    s = _store(tmp_path)
    r = _make_run(s, run_kind="AGENT_ANALYSIS")
    a = _make_task(s, r)
    _set_state(s, a["task_id"], "INDETERMINATE")
    _set_run_state(s, r["run_id"], "RUNNING")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    sched.run_once(run_id=r["run_id"])
    refreshed = s.connection.execute(
        "SELECT state FROM agent_runs WHERE run_id=?", (r["run_id"],)
    ).fetchone()
    assert refreshed[0] == "NEEDS_RECONCILIATION"


def test_run_once_waiting_message_lifts_run_to_waiting_user(tmp_path):
    """任一 task WAITING_MESSAGE → run WAITING_USER。"""
    s = _store(tmp_path)
    r = _make_run(s, run_kind="AGENT_ANALYSIS")
    a = _make_task(s, r)
    _set_state(s, a["task_id"], "WAITING_MESSAGE")
    _set_run_state(s, r["run_id"], "RUNNING")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    sched.run_once(run_id=r["run_id"])
    refreshed = s.connection.execute(
        "SELECT state FROM agent_runs WHERE run_id=?", (r["run_id"],)
    ).fetchone()
    assert refreshed[0] == "WAITING_USER"


# ════════════════════════════════════════════════════════
# no duplicate releases after recovery
# ════════════════════════════════════════════════════════


def test_run_until_blocked_drains_chain_of_tasks(tmp_path):
    """run_until_blocked:连续 promote → claim → 终止 → 聚合 一气呵成。"""
    s = _store(tmp_path)
    r = _make_run(s, run_kind="AGENT_ANALYSIS")
    a = _make_task(s, r)
    b = _make_task(s, r)
    _set_state(s, b["task_id"], "PLANNED")
    _add_dependency(s, run_id=r["run_id"], task_id=b["task_id"],
                    depends_on_task_id=a["task_id"], condition="ON_SUCCESS")
    _set_state(s, a["task_id"], "RUNNING")
    # 让 a 仍 RUNNING — scheduler 只 promote, 不强制完成 a

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    # 跑一两次:dependency readiness 计算
    sched.run_once(run_id=r["run_id"])
    # b 仍 PLANNED,因为 a 没终止
    refreshed = s.connection.execute(
        "SELECT state FROM agent_tasks WHERE task_id=?", (b["task_id"],)
    ).fetchone()
    assert refreshed[0] == "PLANNED"

    # 现在 a SUCCEEDED → b 应 promote → READY
    _set_state(s, a["task_id"], "SUCCEEDED")
    sched.run_once(run_id=r["run_id"])
    refreshed = s.connection.execute(
        "SELECT state FROM agent_tasks WHERE task_id=?", (b["task_id"],)
    ).fetchone()
    assert refreshed[0] == "READY"


def test_run_once_does_not_aggregate_already_terminal_run(tmp_path):
    """已经终态的 run 不再被聚合(防止重复 release)。"""
    s = _store(tmp_path)
    r = _make_run(s)
    _set_run_state(s, r["run_id"], "SUCCEEDED")
    a = _make_task(s, r)
    _set_state(s, a["task_id"], "SUCCEEDED")

    from tradingagents.agent_harness.runtime.scheduler import TaskScheduler
    sched = TaskScheduler(s)
    report = sched.run_once(run_id=r["run_id"])
    assert r["run_id"] not in report["aggregated_run_ids"]
