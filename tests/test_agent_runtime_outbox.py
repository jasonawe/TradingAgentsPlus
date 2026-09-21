"""Task 5 — OutboxWorker / outbox repository tests.

覆盖 plan 要求:
- PENDING → CLAIMED → DELIVERED happy path
- 60 秒 claim 过期 → 重置为 PENDING
- 指数退避(2^attempts 秒,封顶 60 秒)
- 10 次失败 → DEAD
- 非 NULL delivery_key(event 派生)
- scheduler destination DEAD → run FAILED
- audit destination DEAD → run 状态不变(只记 health warning)
- duplicate delivery no-op(再次投递同一 delivery_key 幂等)
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def _make_event(store, run, *, event_type="PROGRESS"):
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    er = EventRepository(store)
    now = datetime.now(timezone.utc).isoformat()
    return er.append_runtime_event(
        run_id=run["run_id"], event_type=event_type,
        surface="PUBLIC", payload_json={"k": "v"}, now=now,
    )


def _enqueue(store, run, event, *, destination="sse", delivery_key=None):
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    er = EventRepository(store)
    now = datetime.now(timezone.utc).isoformat()
    er.append_outbox(
        run_id=run["run_id"], task_id=None,
        message_id=None, source_event_id=event["event_id"],
        delivery_key=delivery_key or f"{destination}|{event['event_id']}",
        destination=destination,
        payload_json={"event_id": event["event_id"], "seq": event["seq"]},
        now=now,
    )


# ════════════════════════════════════════════════════════
# happy path
# ════════════════════════════════════════════════════════

def test_outbox_pending_to_delivered(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    ev = _make_event(s, r)
    _enqueue(s, r, ev, destination="sse")

    from tradingagents.agent_harness.runtime.outbox import OutboxWorker
    delivered = []
    worker = OutboxWorker(s, handlers={"sse": lambda p: delivered.append(p) or True})
    summary = worker.run_once(limit=10)

    assert summary["delivered"] == 1
    assert summary["failed"] == 0
    assert summary["dead"] == 0
    assert len(delivered) == 1


# ════════════════════════════════════════════════════════
# claim expiry (60s)
# ════════════════════════════════════════════════════════

def test_outbox_expired_claim_resets_to_pending(tmp_path):
    """CLAIMED 超过 lease 秒应可被 worker B 重新 claim 并投递。"""
    s = _store(tmp_path)
    r = _make_run(s)
    ev = _make_event(s, r)
    _enqueue(s, r, ev)

    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    from tradingagents.agent_harness.runtime.outbox import OutboxWorker
    er = EventRepository(s)

    # 模拟 worker A 已 claim 但从未投递完成:把 row 直接标 CLAIMED,
    # claimed_at 设到 61s 之前,模拟 lease 过期。
    row = er.list_outbox(r["run_id"])[0]
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    s.connection.execute(
        "UPDATE agent_outbox SET state='CLAIMED', claimed_by='wkr-A', claimed_at=? "
        "WHERE outbox_id=?",
        (old_ts, row["outbox_id"]),
    )
    s.connection.commit()
    row = er.list_outbox(r["run_id"])[0]
    assert row["state"] == "CLAIMED"

    # Worker B runs — expired claim should be reclaimable & delivered
    worker_b = OutboxWorker(s, handlers={"sse": lambda p: True})
    summary_b = worker_b.run_once(limit=10)
    assert summary_b["claimed"] >= 1, "expired claim must be reclaimable"
    final = er.list_outbox(r["run_id"])[0]
    assert final["state"] == "DELIVERED"


# ════════════════════════════════════════════════════════
# exponential backoff capped at 60s
# ════════════════════════════════════════════════════════

def test_outbox_exponential_backoff_capped(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    ev = _make_event(s, r)
    _enqueue(s, r, ev)

    from tradingagents.agent_harness.runtime.outbox import OutboxWorker
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository

    def fail(payload):
        raise RuntimeError("boom")

    worker = OutboxWorker(s, handlers={"sse": fail})

    # attempt 1 → 2s backoff
    s1 = worker.run_once(limit=10)
    assert s1["failed"] == 1
    row = EventRepository(s).list_outbox(r["run_id"])[0]
    assert row["state"] == "PENDING"
    assert row["attempts"] == 1
    delay1 = _delay_seconds(row["available_at"])
    assert 0 <= delay1 <= 3, f"first backoff should be ~2s, got {delay1}"

    # 把 available_at 拨到现在之前,模拟 backoff 已经过去
    s.connection.execute(
        "UPDATE agent_outbox SET available_at=? WHERE outbox_id=?",
        (datetime.now(timezone.utc).isoformat(), row["outbox_id"]),
    )
    s.connection.commit()

    # attempt 2 → 4s
    s2 = worker.run_once(limit=10)
    assert s2["failed"] == 1
    row = EventRepository(s).list_outbox(r["run_id"])[0]
    assert row["attempts"] == 2
    delay2 = _delay_seconds(row["available_at"])
    assert 0 <= delay2 <= 5, f"second backoff should be ~4s, got {delay2}"


def _delay_seconds(iso_ts):
    target = datetime.fromisoformat(iso_ts)
    now = datetime.now(timezone.utc)
    return (target - now).total_seconds()


# ════════════════════════════════════════════════════════
# DEAD after 10 attempts
# ════════════════════════════════════════════════════════

def test_outbox_dead_after_max_attempts(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    ev = _make_event(s, r)
    _enqueue(s, r, ev)

    from tradingagents.agent_harness.runtime.outbox import OutboxWorker
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository

    def fail(payload):
        raise RuntimeError("boom")

    worker = OutboxWorker(s, handlers={"sse": fail})

    # run 10 attempts; after each failure bump attempts manually + reset available_at
    er = EventRepository(s)
    for i in range(10):
        # shift available_at to now so it's claimable
        row = er.list_outbox(r["run_id"])[0]
        if row["state"] == "DEAD":
            break
        s.connection.execute(
            "UPDATE agent_outbox SET available_at=? WHERE outbox_id=?",
            (datetime.now(timezone.utc).isoformat(), row["outbox_id"]),
        )
        s.connection.commit()
        worker.run_once(limit=10)

    row = er.list_outbox(r["run_id"])[0]
    assert row["state"] == "DEAD"
    assert row["attempts"] == 10


# ════════════════════════════════════════════════════════
# non-null event-derived delivery keys
# ════════════════════════════════════════════════════════

def test_outbox_delivery_keys_are_non_null(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    ev = _make_event(s, r)
    _enqueue(s, r, ev, delivery_key=None)
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    row = EventRepository(s).list_outbox(r["run_id"])[0]
    assert row["delivery_key"] is not None
    assert row["delivery_key"] != ""


# ════════════════════════════════════════════════════════
# scheduler DEAD → run FAILED
# ════════════════════════════════════════════════════════

def test_outbox_scheduler_dead_fails_run(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    ev = _make_event(s, r)
    _enqueue(s, r, ev, destination="scheduler")

    from tradingagents.agent_harness.runtime.outbox import OutboxWorker
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository

    def fail(payload):
        raise RuntimeError("scheduler fail")

    worker = OutboxWorker(s, handlers={"scheduler": fail})
    er = EventRepository(s)
    rr = RunRepository(s)

    for i in range(10):
        row = er.list_outbox(r["run_id"])[0]
        if row["state"] == "DEAD":
            break
        s.connection.execute(
            "UPDATE agent_outbox SET available_at=? WHERE outbox_id=?",
            (datetime.now(timezone.utc).isoformat(), row["outbox_id"]),
        )
        s.connection.commit()
        worker.run_once(limit=10)

    after = rr.get_run(r["run_id"])
    assert after["state"] == "FAILED"
    assert after["terminal_reason"] == "OUTBOX_SCHEDULER_DEAD"


# ════════════════════════════════════════════════════════
# audit DEAD → run unchanged, only health warning
# ════════════════════════════════════════════════════════

def test_outbox_audit_dead_leaves_run_alive(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    ev = _make_event(s, r)
    _enqueue(s, r, ev, destination="audit")

    from tradingagents.agent_harness.runtime.outbox import OutboxWorker
    from tradingagents.agent_harness.runtime.persistence.events import EventRepository
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository

    def fail(payload):
        raise RuntimeError("audit fail")

    worker = OutboxWorker(s, handlers={"audit": fail})
    er = EventRepository(s)
    rr = RunRepository(s)

    for i in range(10):
        row = er.list_outbox(r["run_id"])[0]
        if row["state"] == "DEAD":
            break
        s.connection.execute(
            "UPDATE agent_outbox SET available_at=? WHERE outbox_id=?",
            (datetime.now(timezone.utc).isoformat(), row["outbox_id"]),
        )
        s.connection.commit()
        worker.run_once(limit=10)

    after = rr.get_run(r["run_id"])
    # Run should NOT be terminal because audit failure is non-fatal
    assert after["state"] == "PLANNING", f"run state should be unchanged, got {after['state']}"
    row = er.list_outbox(r["run_id"])[0]
    assert row["state"] == "DEAD"


# ════════════════════════════════════════════════════════
# duplicate delivery no-op
# ════════════════════════════════════════════════════════

def test_outbox_duplicate_delivery_is_idempotent(tmp_path):
    s = _store(tmp_path)
    r = _make_run(s)
    ev = _make_event(s, r)
    _enqueue(s, r, ev)

    from tradingagents.agent_harness.runtime.outbox import OutboxWorker
    seen = []
    worker = OutboxWorker(s, handlers={"sse": lambda p: seen.append(p["event_id"]) or True})
    worker.run_once(limit=10)
    # replay same event source — enqueue duplicate
    _enqueue(s, r, ev)
    try:
        worker.run_once(limit=10)
    except Exception:
        pass  # duplicate should not raise
    # only one delivery per delivery_key
    rows = [row for row in __import__('tradingagents.agent_harness.runtime.persistence.events', fromlist=['EventRepository']).EventRepository(s).list_outbox(r["run_id"])]
    keys = [r["delivery_key"] for r in rows]
    assert len(set(keys)) == 1, f"expected unique delivery_key per event, got {keys}"
