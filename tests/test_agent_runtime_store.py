"""Task 4 — atomic transactional store tests。

覆盖 plan 要求:
- one active run per session(active 唯一性)
- strict state/version CAS(状态/版本乐观锁)
- atomic AgentMessage + runtime_event + outbox(同事务)
- monotonic run seq under concurrent threads
- graph revision CAS
- accepted/rejected GraphPatch
- wait persistence + resolution after restart
- run aggregation by run kind
- terminal_seq
- 故障注入:exception 后 message/transition/outbox 全部 rollback
- 并发竞态:两个 task completion 只一个 CAS 赢
- legacy replacement 唯一性
"""
from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace
import uuid
from pathlib import Path

import pytest


def _import_models():
    from tradingagents.agent_harness.runtime.models import (
        AgentMessageDraft, AgentMessageType, AgentTask, AgentReply,
        TaskState, RunState, RunKind, TaskKind, TaskPayload, ResultPayload,
    )
    return AgentMessageDraft, AgentMessageType, AgentTask, AgentReply, \
        TaskState, RunState, RunKind, TaskKind, TaskPayload, ResultPayload


def _store(tmp_path):
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    return AgentRuntimeStore(tmp_path / "runtime.sqlite")


def _make_run(store, *, session_id="sess-1", run_kind="AGENT_ANALYSIS"):
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    rr = RunRepository(store)
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["600036.SS"], carry_symbols=[],
        slots={}, tier=2, confidence=1.0,
        reason_code="test", route_kind="DIRECT_READ",
    )
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    return rr.create_run(
        run_id=str(uuid.uuid4()),
        session_id=session_id,
        turn_id="turn-1",
        run_kind=run_kind,
        route=route,
        budgets={"max_llm_calls": 10},
        now=now,
    )


def _make_task(store, run, *, task_kind="PLANNER", state="READY"):
    """直接用 dict-like payload(避免 AgentTask 必填字段太多)。"""
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(store)
    class _FakePayload:
        objective = "plan"
        inputs = {}
    payload = _FakePayload()
    run_id = run["run_id"] if isinstance(run, dict) else run.run_id
    run_turn = run["turn_id"] if isinstance(run, dict) else run.turn_id
    task = tr.create_task(
        task_id=str(uuid.uuid4()),
        run_id=run_id,
        parent_task_id=None,
        kind=task_kind,
        agent_name=None,
        system_handler=None,
        capability="planning",
        payload=payload,
        required=True,
        max_execution_attempts=3,
        now=run_turn,
    )
    if state != "READY":
        task = tr.transition_task(
            task_id=task["task_id"],
            expected_version=task["version"],
            expected_state="READY",
            new_state=state,
            now=run_turn,
        )
    return task

# ════════════════════════════════════════════════════════
# one active run per session
# ════════════════════════════════════════════════════════

def test_one_active_run_per_session(tmp_path):
    s = _store(tmp_path)
    r1 = _make_run(s, session_id="sess-A")
    r2 = _make_run(s, session_id="sess-B")
    # 同 session 不能再开 active run
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    rr = RunRepository(s)
    with pytest.raises(Exception) as exc:
        rr.create_run(
            run_id=str(uuid.uuid4()),
            session_id="sess-A",
            turn_id="turn-2",
            run_kind="AGENT_ANALYSIS",
            route=r1["route"] if isinstance(r1, dict) else r1["route"],
            budgets={"max_llm_calls": 10},
            now=r1["created_at"] if isinstance(r1, dict) else r1["created_at"],
        )
    assert "active" in str(exc.value).lower() or "unique" in str(exc.value).lower()


# ════════════════════════════════════════════════════════
# strict state/version CAS
# ════════════════════════════════════════════════════════

def test_task_state_cas_succeeds(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, state="READY")
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(s)
    updated = tr.transition_task(
        task_id=t["task_id"], expected_version=t["version"],
        expected_state="READY", new_state="RUNNING",
        now=r["created_at"],
    )
    assert updated["state"] == "RUNNING"
    assert updated["version"] == t["version"] + 1


def test_task_state_cas_rejects_stale_version(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, state="READY")
    # 先 transition 一次
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(s)
    updated = tr.transition_task(
        task_id=t["task_id"], expected_version=t["version"],
        expected_state="READY", new_state="RUNNING", now=r["created_at"],
    )
    # 用旧 version 再 transition 应失败
    with pytest.raises(Exception) as exc:
        tr.transition_task(
            task_id=t["task_id"], expected_version=t["version"],  # 旧版本
            expected_state="RUNNING", new_state="SUCCEEDED",
            now=r["created_at"],
        )
    assert "version" in str(exc.value).lower() or "cas" in str(exc.value).lower() or "stale" in str(exc.value).lower()


def test_task_state_cas_rejects_wrong_state(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, state="READY")
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(s)
    with pytest.raises(Exception):
        tr.transition_task(
            task_id=t["task_id"], expected_version=t["version"],
            expected_state="RUNNING",  # 错状态
            new_state="SUCCEEDED", now=r["created_at"],
        )


# ════════════════════════════════════════════════════════
# atomic AgentMessage + runtime_event + outbox
# ════════════════════════════════════════════════════════

def test_append_message_event_outbox_atomically(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, state="RUNNING")
    _, AgentMessageType, _, _, TaskState, RunState, _, _, _, _ = _import_models()
    draft = SimpleNamespace(
        run_id=r["run_id"], turn_id=r["turn_id"], task_id=t["task_id"],
        parent_task_id=None, sender="PlannerAgent",
        recipient="VerifierAgent", type=AgentMessageType.RESULT,
        payload={"ok": True}, evidence_refs=[],
        correlation_id=str(uuid.uuid4()),
        execution_attempt=1,
    )
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    er = EventRepository(s)
    stored = er.append_message_and_outbox(
        draft=draft, causation_id=None,
        outbox_destinations=("sse", "audit"),
        delivery_keys={"sse": "k1", "audit": "k2"},
        now=r["created_at"],
    )
    assert stored.message["seq"] == stored.event["seq"]
    assert stored.event["run_id"] == r["run_id"]
    # outbox 应该有 2 行
    out = er.list_outbox(r["run_id"])
    assert len(out) == 2
    assert all(o["source_event_id"] == stored.event["event_id"] for o in out)
    # delivery_key unique per destination
    assert {(o["destination"], o["delivery_key"]) for o in out} == {("sse", "k1"), ("audit", "k2")}


# ════════════════════════════════════════════════════════
# monotonic run seq under concurrent threads
# ════════════════════════════════════════════════════════

def test_monotonic_seq_under_concurrent_threads(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    seqs = []
    lock = threading.Lock()
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    er = EventRepository(s)

    def worker(idx):
        ev = er.append_runtime_event(
            run_id=r["run_id"], event_type=f"PROGRESS_{idx}",
            surface="PUBLIC", payload_json={"i": idx},
            now=r["created_at"],
        )
        with lock:
            seqs.append(ev["seq"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # seq 严格递增 + 唯一
    assert len(seqs) == 10
    assert len(set(seqs)) == 10
    assert seqs == sorted(seqs)


# ════════════════════════════════════════════════════════
# graph revision CAS
# ════════════════════════════════════════════════════════

def test_graph_revision_cas(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    assert r["graph_revision"] == 0
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    rr = RunRepository(s)
    new_rev = rr.bump_graph_revision(run_id=r["run_id"], expected_revision=0, now=r["created_at"])
    assert new_rev == 1
    with pytest.raises(Exception):
        rr.bump_graph_revision(run_id=r["run_id"], expected_revision=0, now=r["created_at"])


# ════════════════════════════════════════════════════════
# accepted/rejected GraphPatch
# ════════════════════════════════════════════════════════

def test_graph_patch_adds_child_task(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, state="RUNNING")
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(s)
    child = tr.add_graph_patch_child(
        run_id=r["run_id"], parent_task_id=t["task_id"],
        kind="REPAIR", agent_name="VerifierAgent",
        capability="verifying",
        payload_objective="verify",
        payload_inputs={"artifact": "x"},
        expected_graph_revision=0,
        now=r["created_at"],
    )
    assert child["parent_task_id"] == t["task_id"]
    # graph revision 应该 bump
    r_after = s.connection.execute(
        "SELECT graph_revision FROM agent_runs WHERE run_id=?", (r["run_id"],)
    ).fetchone()
    assert r_after[0] == 1


# ════════════════════════════════════════════════════════
# wait persistence + resolution
# ════════════════════════════════════════════════════════

def test_wait_creation_and_resolution(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t1 = _make_task(s, r, task_kind="PARENT")
    t2 = _make_task(s, r, task_kind="CHILD")
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(s)
    wait = tr.create_wait(
        run_id=r["run_id"], waiter_task_id=t1["task_id"], child_task_id=t2["task_id"],
        wait_kind="CHILD_TASK", failure_policy="FAIL_RUN",
        now=r["created_at"],
    )
    assert wait["state"] == "WAITING"
    resolved = tr.resolve_wait(
        wait_id=wait["wait_id"], child_state="SUCCEEDED",
        now=r["created_at"],
    )
    assert resolved["state"] == "RESOLVED"
    assert resolved["resolved_at"] is not None


def test_wait_resolution_after_restart(tmp_path):
    """重启后 wait 状态应可读 + 可 resolve。"""
    s = _store(tmp_path)
    r = _make_run(s)
    t1 = _make_task(s, r, task_kind="PARENT")
    t2 = _make_task(s, r, task_kind="CHILD")
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(s)
    wait = tr.create_wait(
        run_id=r["run_id"], waiter_task_id=t1["task_id"], child_task_id=t2["task_id"],
        wait_kind="CHILD_TASK", failure_policy="FAIL_RUN",
        now=r["created_at"],
    )
    wait_id = wait["wait_id"]
    # 模拟重启 — 新 store 实例读同一文件
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    s2 = AgentRuntimeStore(tmp_path / "runtime.sqlite")
    tr2 = TaskRepository(s2)
    loaded = tr2.get_wait(wait_id)
    assert loaded["state"] == "WAITING"
    resolved = tr2.resolve_wait(wait_id=wait_id, child_state="SUCCEEDED", now=r["created_at"])
    assert resolved["state"] == "RESOLVED"


# ════════════════════════════════════════════════════════
# run aggregation by run kind
# ════════════════════════════════════════════════════════

def test_list_runs_by_kind(tmp_path):
    s = _store(tmp_path)
    _make_run(s, session_id="s1", run_kind="AGENT_ANALYSIS")
    _make_run(s, session_id="s2", run_kind="SYSTEM_COMMAND")
    _make_run(s, session_id="s3", run_kind="AGENT_ANALYSIS")
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    rr = RunRepository(s)
    tier2 = rr.list_runs(run_kind="AGENT_ANALYSIS")
    assert len(tier2) == 2
    assert all(r["run_kind"] == "AGENT_ANALYSIS" for r in tier2)


# ════════════════════════════════════════════════════════
# terminal_seq
# ════════════════════════════════════════════════════════

def test_terminal_seq_set_on_run_terminal(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    rr = RunRepository(s)
    rr.mark_terminal(run_id=r["run_id"], expected_state="PLANNING", final_seq=5,
                    final_result_json={"ok": True}, terminal_reason="COMPLETED",
                    now=r["created_at"])
    after = rr.get_run(r["run_id"])
    assert after["terminal_seq"] == 5
    # COMPLETED 映射到规范终态 SUCCEEDED
    assert after["state"] == "SUCCEEDED"
    assert after["terminal_reason"] == "COMPLETED"


# ════════════════════════════════════════════════════════
# 故障注入:exception after insert → 全 rollback
# ════════════════════════════════════════════════════════

def test_atomic_rollback_on_mid_transaction_failure(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, state="RUNNING")
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    er = EventRepository(s)

    # monkey-patch append_outbox 让它抛错,验证 message / event 也回滚
    orig = er.append_outbox
    def boom(*args, **kwargs):
        raise RuntimeError("simulated outbox failure")
    er.append_outbox = boom

    try:
        with pytest.raises(RuntimeError):
            er.append_message_and_outbox(
                draft=None,  # 不应跑到
                outbox_destinations=("sse",),
                delivery_keys={"sse": "k"},
                now=r["created_at"],
            )
    finally:
        er.append_outbox = orig

    # 验证:没有 message / event 被持久化
    n_msg = s.connection.execute(
        "SELECT COUNT(*) FROM agent_messages WHERE run_id=?", (r["run_id"],)
    ).fetchone()[0]
    n_evt = s.connection.execute(
        "SELECT COUNT(*) FROM runtime_events WHERE run_id=?", (r["run_id"],)
    ).fetchone()[0]
    n_out = s.connection.execute(
        "SELECT COUNT(*) FROM agent_outbox WHERE run_id=?", (r["run_id"],)
    ).fetchone()[0]
    assert n_msg == 0, "message 应该被 rollback"
    assert n_evt == 0, "event 应该被 rollback"
    assert n_out == 0, "outbox 应该被 rollback"


# ════════════════════════════════════════════════════════
# 并发竞态:两个 task completion → 只一个 CAS 赢
# ════════════════════════════════════════════════════════

def test_concurrent_task_completion_only_one_cas_wins(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, state="RUNNING")
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(s)
    results = []
    lock = threading.Lock()

    def worker():
        try:
            tr.transition_task(
                task_id=t["task_id"], expected_version=t["version"],
                expected_state="RUNNING", new_state="SUCCEEDED",
                now=r["created_at"],
            )
            with lock:
                results.append("win")
        except Exception:
            with lock:
                results.append("lose")

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    # 只一个 win,其他全 lose
    wins = results.count("win")
    loses = results.count("lose")
    assert wins == 1
    assert loses == 4


# ════════════════════════════════════════════════════════
# legacy replacement 唯一性
# ════════════════════════════════════════════════════════

def test_legacy_replacement_unique(tmp_path):
    s = _store(tmp_path)
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    rr = RunRepository(s)
    legacy_run = _make_run(s, session_id="legacy-sess")
    # 创建两个 replacement run_id
    rep1 = str(uuid.uuid4())
    rep2 = str(uuid.uuid4())
    rr.register_legacy_replacement(
        legacy_store_identity="orch_legacy",
        session_id="legacy-sess",
        workflow_name="default",
        milestone_id="step1",
        node_position="node1",
        original_user_message="hello",
        state="REPLACED",
        replacement_run_id=rep1,
        now=legacy_run["created_at"],
    )
    # 同 migration_key 不应允许
    with pytest.raises(Exception):
        rr.register_legacy_replacement(
            legacy_store_identity="orch_legacy",
            session_id="legacy-sess-2",
            workflow_name="default",
            milestone_id="step1",
            node_position="node1",
            original_user_message="hello",
            state="REPLACED",
            replacement_run_id=rep2,
            now=legacy_run["created_at"],
        )


# ════════════════════════════════════════════════════════
# active / latest recoverable runs
# ════════════════════════════════════════════════════════

def test_list_active_runs_filters_correctly(tmp_path):
    s = _store(tmp_path)
    r1 = _make_run(s, session_id="s1")
    r2 = _make_run(s, session_id="s2")
    r3 = _make_run(s, session_id="s3")
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    rr = RunRepository(s)
    # 把 r2 标 terminal
    rr.mark_terminal(run_id=r2["run_id"], expected_state="PLANNING", final_seq=1,
                    final_result_json=None, terminal_reason="COMPLETED",
                    now=r1["created_at"])
    active = rr.list_active_runs()
    ids = {r["run_id"] for r in active}
    assert r1["run_id"] in ids
    assert r2["run_id"] not in ids
    assert r3["run_id"] in ids


def test_latest_recoverable_run_per_session(tmp_path):
    s = _store(tmp_path)
    r1 = _make_run(s, session_id="sX")
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    rr = RunRepository(s)
    rr.mark_terminal(run_id=r1["run_id"], expected_state="PLANNING", final_seq=1,
                    final_result_json=None, terminal_reason="FAILED",
                    now=r1["created_at"])
    # 创建新 run 同 session(可以,因为 r1 已 terminal)
    r2 = _make_run(s, session_id="sX")
    latest = rr.get_latest_recoverable_run("sX")
    assert latest is not None
    assert latest["run_id"] == r2["run_id"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
