"""O10 — audit log must record actual write outcomes (Bug 1 regression).

Before the fix:
- ``write_audit_log`` only ever contained ``pending`` and ``confirmed``
  rows because neither the harness confirm endpoint nor
  ``_after_execute`` ever called ``update_write_status(... status='executed')``
  or ``status='failed'``.
- The DB evidence (757 pending / 157 confirmed / 0 executed / 0 failed)
  made auditing destructive operations impossible.

After the fix:
- ``_after_execute(session_id, tool_name, tool_args, audit_id=..., error=...)``
  writes ``executed`` on success and ``failed`` (with the error message) on
  failure.
- ``web/app.py`` confirm endpoints update the audit row to ``executed`` or
  ``failed`` after the tool actually runs.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, '/Users/shenkang/workroom/languages/agent/TradingAgents')

import pytest

from tradingagents.agent_harness.audit import (
    log_write,
    list_writes,
    update_write_status,
)
from tradingagents.agent_harness.hitl import (
    grant_approval,
    is_approved,
    consume_approval,
    revoke_session,
)


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """Use a temp DB so the test never touches the real audit log."""
    db_file = tmp_path / "audit_test.sqlite3"
    # Apply migration 011 so the table exists
    import sqlite3
    conn = sqlite3.connect(str(db_file))
    sql = Path("web/migrations/011_agent_memory.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    conn.close()

    # Re-point audit._default_db_path at the temp file for the test
    from tradingagents.agent_harness import audit as _audit
    monkeypatch.setattr(_audit, "_default_db_path", lambda: db_file)
    yield db_file


def test_after_execute_marks_audit_executed(isolated_db):
    """_after_execute with audit_id writes 'executed' to audit log."""
    from tradingagents.agent_harness.tools.impl import _after_execute

    session_id = "test-s-exec"
    revoke_session(session_id)
    grant_approval(session_id, "create_note", {"symbol": "600036.SS"})

    audit_id = log_write(
        tool_name="create_note",
        tool_args={"symbol": "600036.SS"},
        status="pending",
    )

    _after_execute(session_id, "create_note", {"symbol": "600036.SS"}, audit_id=audit_id)

    rows = list_writes(db_path=isolated_db, session_id=None, limit=10)
    target = next(r for r in rows if r["id"] == audit_id)
    assert target["status"] == "executed", f"got status={target['status']!r}"
    assert target["error"] is None
    # approval must be consumed
    assert is_approved(session_id, "create_note", {"symbol": "600036.SS"}) is False


def test_after_execute_marks_audit_failed(isolated_db):
    """_after_execute with error writes 'failed' to audit log."""
    from tradingagents.agent_harness.tools.impl import _after_execute

    session_id = "test-s-fail"
    revoke_session(session_id)
    grant_approval(session_id, "delete_note", {"note_id": "n1"})

    audit_id = log_write(
        tool_name="delete_note",
        tool_args={"note_id": "n1"},
        status="pending",
    )

    _after_execute(
        session_id,
        "delete_note",
        {"note_id": "n1"},
        audit_id=audit_id,
        error="boom: note not found",
    )

    rows = list_writes(db_path=isolated_db, limit=10)
    target = next(r for r in rows if r["id"] == audit_id)
    assert target["status"] == "failed", f"got status={target['status']!r}"
    assert target["error"] == "boom: note not found"
    # approval still consumed — failed ops shouldn't replay either
    assert is_approved(session_id, "delete_note", {"note_id": "n1"}) is False


def test_after_execute_without_audit_id_is_noop(isolated_db):
    """_after_execute with audit_id=None must not raise and must still consume."""
    from tradingagents.agent_harness.tools.impl import _after_execute

    session_id = "test-s-noaudit"
    revoke_session(session_id)
    grant_approval(session_id, "create_note", {"symbol": "X"})

    # Should not raise
    _after_execute(session_id, "create_note", {"symbol": "X"}, audit_id=None)

    # Approval should still be consumed even without an audit id
    assert is_approved(session_id, "create_note", {"symbol": "X"}) is False


def test_after_execute_audit_write_error_doesnt_crash(isolated_db, monkeypatch):
    """If the audit DB write fails, _after_execute must swallow the error."""
    from tradingagents.agent_harness.tools.impl import _after_execute

    session_id = "test-s-audit-err"
    revoke_session(session_id)
    grant_approval(session_id, "create_note", {"symbol": "Z"})

    # Force update_write_status to raise
    def boom(*a, **kw):
        raise RuntimeError("simulated audit DB down")

    monkeypatch.setattr(
        "tradingagents.agent_harness.audit.update_write_status", boom,
    )

    # Should not raise even though the audit update fails
    _after_execute(session_id, "create_note", {"symbol": "Z"}, audit_id=42)
    # Approval should still be consumed
    assert is_approved(session_id, "create_note", {"symbol": "Z"}) is False


def test_full_lifecycle_pending_to_executed(isolated_db):
    """End-to-end: log_write(pending) -> confirmed -> executed -> list_writes sees all 3."""
    audit_id = log_write(
        tool_name="create_alert",
        tool_args={"symbol": "600036.SS", "kind": "price", "params": {"threshold": 1.0}},
        session_id="lifecycle-1",
        status="pending",
    )

    update_write_status(isolated_db, audit_id, status="confirmed", confirmed_by="user")
    update_write_status(isolated_db, audit_id, status="executed")

    rows = list_writes(db_path=isolated_db, session_id="lifecycle-1")
    target = next(r for r in rows if r["id"] == audit_id)
    assert target["status"] == "executed"
    assert target["confirmed_by"] == "user"
    assert target["error"] is None


def test_audit_module_writes_match_db(isolated_db):
    """list_writes returns the same status that was just written (no caching surprises)."""
    audit_id = log_write(
        tool_name="delete_note",
        tool_args={"note_id": "n-99"},
        session_id="match-1",
        status="pending",
    )
    update_write_status(isolated_db, audit_id, status="confirmed", confirmed_by="u1")
    update_write_status(isolated_db, audit_id, status="executed")

    rows = list_writes(db_path=isolated_db, session_id="match-1", limit=5)
    assert len(rows) >= 1
    target = rows[0]  # most recent first
    assert target["id"] == audit_id
    assert target["status"] == "executed"
    assert target["confirmed_by"] == "u1"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
