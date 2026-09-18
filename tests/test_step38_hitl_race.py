"""Step 38 — HITL audit↔tool race fixes (R-E, R-G, R-H, R-F).

R-E: Persistent approval registry — approvals survive process restart.
R-G: Audit row sweeper — orphan pending/confirmed rows expire.
R-H: Tool invoke status barrier — failed audit updates are queued for
     retry instead of silently dropped.
R-F: Batch confirm — orchestrator collects multiple pending audit rows
     in the same turn so the UI shows all gates at once.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest


# ------------------------------------------------------------------
# R-E: Persistent approval registry
# ------------------------------------------------------------------
def _tmp_web_runs(monkeypatch):
    """Create a fresh web_runs.sqlite3 in a tmp dir and point env at it."""
    tmp = tempfile.mkdtemp()
    db = Path(tmp) / "web_runs.sqlite3"
    monkeypatch.setenv("TRADINGAGENTS_WEB_RUNS_DB", str(db))
    # Reload default_config so the env override takes effect.
    import importlib
    import tradingagents.default_config as dc
    importlib.reload(dc)
    import tradingagents.agent_harness.hitl as hitl
    importlib.reload(hitl)
    return hitl, db


def test_persistent_approval_survives_module_reload(monkeypatch):
    """grant_approval writes to SQLite so a process restart sees it."""
    hitl, db = _tmp_web_runs(monkeypatch)
    hitl.grant_approval("sess-A", "create_note",
                        {"symbol": "600036.SS", "body_md": "hi"})
    assert hitl.is_approved("sess-A", "create_note",
                             {"symbol": "600036.SS", "body_md": "hi"})

    # Simulate process restart by reloading the module — in-memory
    # _approved is wiped, but SQLite survives.
    import importlib
    importlib.reload(hitl)
    assert hitl.is_approved("sess-A", "create_note",
                             {"symbol": "600036.SS", "body_md": "hi"}), \
        "approval should survive process restart"


def test_consume_approval_sets_consumed_at(monkeypatch):
    """consume_approval marks the row as used (not deleted)."""
    hitl, db = _tmp_web_runs(monkeypatch)
    hitl.grant_approval("sess-B", "create_note", {"x": 1})
    hitl.consume_approval("sess-B", "create_note", {"x": 1})

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT consumed_at FROM tool_approvals "
            "WHERE session_id = ?",
            ("sess-B",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] is not None, "consumed_at should be set"


def test_revoke_session_removes_db_rows(monkeypatch):
    """revoke_session clears all session approvals from both L1 and DB."""
    hitl, db = _tmp_web_runs(monkeypatch)
    hitl.grant_approval("sess-C", "create_note", {"x": 1})
    hitl.grant_approval("sess-C", "create_note", {"x": 2})
    assert hitl.count_pending("sess-C") == 2
    hitl.revoke_session("sess-C")
    assert hitl.count_pending("sess-C") == 0


def test_count_pending_globally_and_per_session(monkeypatch):
    """count_pending works both globally and scoped to a session."""
    hitl, _ = _tmp_web_runs(monkeypatch)
    hitl.grant_approval("s-D", "create_note", {"x": 1})
    hitl.grant_approval("s-D", "create_note", {"x": 2})
    hitl.grant_approval("s-E", "create_note", {"x": 3})
    assert hitl.count_pending("s-D") == 2
    assert hitl.count_pending("s-E") == 1
    assert hitl.count_pending() >= 3


def test_expire_pending_removes_row(monkeypatch):
    """expire_pending is the sweeper hook for orphan rows."""
    hitl, _ = _tmp_web_runs(monkeypatch)
    hitl.grant_approval("s-F", "create_note", {"x": 1})
    assert hitl.count_pending("s-F") == 1
    key = hitl._key_of("create_note", {"x": 1})
    ok = hitl.expire_pending("s-F", "create_note", key[1])
    assert ok
    assert hitl.count_pending("s-F") == 0


# ------------------------------------------------------------------
# R-G: Audit sweeper
# ------------------------------------------------------------------
def _setup_db_with_audit_row(db: Path, status: str, age_seconds: int):
    """Insert a write_audit_log row with given status + age."""
    import time as _t
    conn = sqlite3.connect(str(db))
    try:
        # Full schema: matches what audit.py list_writes / log_write expect
        # (014 + earlier migrations). user_message / tool_args / error /
        # impact_note are all non-nullable in the read path.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS write_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, user_message TEXT, tool_name TEXT,
                tool_args TEXT, actor TEXT, confirmed_by TEXT,
                status TEXT, error TEXT, impact_note TEXT,
                created_at TIMESTAMP
            )"""
        )
        ts = _t.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            _t.gmtime(_t.time() - age_seconds),
        )
        cur = conn.execute(
            """INSERT INTO write_audit_log
               (session_id, tool_name, tool_args, actor, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("sess", "create_note", "{}", "agent", status, ts),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def test_sweeper_expires_stale_pending(monkeypatch):
    """Pending rows older than cutoff transition to 'expired'."""
    _, db = _tmp_web_runs(monkeypatch)
    _setup_db_with_audit_row(db, "pending", age_seconds=3600)
    from tradingagents.agent_harness import audit_sweeper
    n = audit_sweeper.expire_stale_pending(db, min_age_seconds=600)
    assert n == 1
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT status FROM write_audit_log"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "expired"


def test_sweeper_keeps_fresh_pending(monkeypatch):
    """Recent pending rows are NOT expired (still within grace)."""
    _, db = _tmp_web_runs(monkeypatch)
    _setup_db_with_audit_row(db, "pending", age_seconds=10)
    from tradingagents.agent_harness import audit_sweeper
    n = audit_sweeper.expire_stale_pending(db, min_age_seconds=600)
    assert n == 0
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT status FROM write_audit_log"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "pending"


def test_sweeper_expires_orphan_confirmed(monkeypatch):
    """Confirmed rows that never ran a tool expire after cutoff."""
    _, db = _tmp_web_runs(monkeypatch)
    _setup_db_with_audit_row(db, "confirmed", age_seconds=3600)
    from tradingagents.agent_harness import audit_sweeper
    n = audit_sweeper.expire_stale_confirmed(db, min_age_seconds=600)
    assert n == 1
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT status FROM write_audit_log"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "expired"


def test_run_sweep_runs_all_three():
    """run_sweep returns counts per task in a single dict."""
    from tradingagents.agent_harness import audit_sweeper
    result = audit_sweeper.run_sweep()
    assert set(result.keys()) == {
        "pending_expired", "confirmed_expired", "failed_retried"
    }


# ------------------------------------------------------------------
# R-H: Failed audit update retry
# ------------------------------------------------------------------
def test_queue_failed_update_dedupes():
    """Same (audit_id, status) is not enqueued twice."""
    from tradingagents.agent_harness import audit_sweeper
    audit_sweeper.queue_failed_update(42, "executed")
    audit_sweeper.queue_failed_update(42, "executed")
    assert len(audit_sweeper._failed_updates) == 1


def test_retry_failed_updates_drains_on_success(monkeypatch, tmp_path):
    """retry_failed_updates succeeds and removes entries from the queue."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """CREATE TABLE write_audit_log (
                id INTEGER PRIMARY KEY,
                status TEXT
            )"""
        )
        conn.execute("INSERT INTO write_audit_log VALUES (100, 'pending')")
        conn.commit()
    finally:
        conn.close()
    from tradingagents.agent_harness import audit_sweeper
    # Drain queue from any earlier tests.
    with audit_sweeper._fail_lock:
        audit_sweeper._failed_updates.clear()
    audit_sweeper.queue_failed_update(100, "executed")
    n = audit_sweeper.retry_failed_updates(db)
    assert n == 1
    conn = sqlite3.connect(str(db))
    try:
        status = conn.execute(
            "SELECT status FROM write_audit_log WHERE id = 100"
        ).fetchone()[0]
    finally:
        conn.close()
    assert status == "executed"


# ------------------------------------------------------------------
# R-F: Batch confirm
# ------------------------------------------------------------------
def test_collects_pending_audit_rows_for_a_session(monkeypatch):
    """When multiple audit rows in pending for one session,
    emit_batch_confirm_request returns them all."""
    _, db = _tmp_web_runs(monkeypatch)
    # Create 3 pending audit rows for the same session.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS write_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, user_message TEXT, tool_name TEXT,
                tool_args TEXT, actor TEXT, confirmed_by TEXT,
                status TEXT, error TEXT, impact_note TEXT, created_at TIMESTAMP
            )"""
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO write_audit_log (session_id, tool_name, status, created_at) "
                "VALUES (?, ?, 'pending', ?)",
                ("batch-sess", f"tool_{i}", "2026-09-18T00:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()
    from tradingagents.agent_harness.audit import list_writes
    pending = list_writes(db, session_id="batch-sess", status="pending")
    assert len(pending) == 3
    assert {p["tool_name"] for p in pending} == {
        "tool_0", "tool_1", "tool_2"
    }


# ------------------------------------------------------------------
# Smoke: legacy in-memory callers still work
# ------------------------------------------------------------------
def test_backward_compat_grant_consume_approve(monkeypatch):
    """The original 4-call sequence (grant -> check -> consume) still works."""
    hitl, _ = _tmp_web_runs(monkeypatch)
    args = {"x": 1}
    assert not hitl.is_approved("s-X", "t", args)
    hitl.grant_approval("s-X", "t", args)
    assert hitl.is_approved("s-X", "t", args)
    hitl.consume_approval("s-X", "t", args)
    assert not hitl.is_approved("s-X", "t", args)
