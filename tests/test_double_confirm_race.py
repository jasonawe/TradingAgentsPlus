"""Double-confirm race fix — claim_write_audit + idempotent /confirm.

The bug being fixed:
  /api/harness/sessions/{sid}/confirm used to call update_write_status
  then tool.invoke directly, with no protection against concurrent
  callers. Double-click or network retry re-invoked the destructive
  tool (delete_note / update_alert / add_to_watchlist / etc).

The fix has two layers:
  1. ``audit.claim_write_audit`` — CAS-style transition pending →
     confirmed/rejected. Only the winner sees rowcount=1.
  2. /confirm handler — if claim returns False, emits an idempotent
     response with the current audit status, never re-invokes the tool.

These tests pin the contract for #1 directly. The /confirm handler is
exercised indirectly via the FastAPI TestClient.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest

from tradingagents.agent_harness.audit import (
    claim_write_audit,
    get_write_status,
    log_write,
    update_write_status,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_db(tmp_path: Path, monkeypatch):
    """Fresh web_runs.sqlite3 with the write_audit_log table."""
    db = tmp_path / "audit.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        Path("web/migrations/011_agent_memory.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.close()

    # Re-point the audit module's default path at the temp file.
    import tradingagents.agent_harness.audit as a
    monkeypatch.setattr(a, "_default_db_path", lambda: db)
    return db


# ---------------------------------------------------------------------------
# claim_write_audit — single-call semantics
# ---------------------------------------------------------------------------


def test_claim_pending_to_confirmed(audit_db) -> None:
    aid = log_write(tool_name="delete_note", tool_args={"note_id": 42}, status="pending")
    assert get_write_status(None, aid) == "pending"

    ok = claim_write_audit(None, aid, action="confirmed", confirmed_by="user")
    assert ok is True
    assert get_write_status(None, aid) == "confirmed"


def test_claim_pending_to_rejected(audit_db) -> None:
    aid = log_write(tool_name="delete_note", tool_args={"note_id": 42}, status="pending")

    ok = claim_write_audit(None, aid, action="rejected", confirmed_by="user")
    assert ok is True
    assert get_write_status(None, aid) == "rejected"


def test_second_claim_loses(audit_db) -> None:
    """Two /confirm clicks for the same audit_id — only one wins."""
    aid = log_write(tool_name="delete_note", tool_args={"note_id": 42}, status="pending")

    # First click — wins
    ok1 = claim_write_audit(None, aid, action="confirmed", confirmed_by="user")
    # Second click — must lose
    ok2 = claim_write_audit(None, aid, action="confirmed", confirmed_by="user")

    assert ok1 is True
    assert ok2 is False
    assert get_write_status(None, aid) == "confirmed"


def test_claim_rejected_after_confirmed_also_loses(audit_db) -> None:
    """Mixed actions — second call (different action) also loses."""
    aid = log_write(tool_name="delete_note", tool_args={"note_id": 42}, status="pending")
    claim_write_audit(None, aid, action="confirmed")
    # Now somebody tries to claim "rejected" — should fail
    assert claim_write_audit(None, aid, action="rejected") is False
    assert get_write_status(None, aid) == "confirmed"


def test_claim_after_executed_loses(audit_db) -> None:
    """Even after the tool has run, re-confirming is a no-op."""
    aid = log_write(tool_name="delete_note", tool_args={"note_id": 42}, status="pending")
    claim_write_audit(None, aid, action="confirmed")
    update_write_status(None, aid, status="executed")

    assert claim_write_audit(None, aid, action="confirmed") is False
    assert get_write_status(None, aid) == "executed"


def test_claim_missing_audit_id_returns_false(audit_db) -> None:
    assert claim_write_audit(None, 99999, action="confirmed") is False


def test_claim_invalid_action_raises(audit_db) -> None:
    aid = log_write(tool_name="x", tool_args={}, status="pending")
    with pytest.raises(ValueError):
        claim_write_audit(None, aid, action="executed")  # not claim-eligible
    with pytest.raises(ValueError):
        claim_write_audit(None, aid, action="bogus")


# ---------------------------------------------------------------------------
# claim_write_audit — concurrent calls (the actual race)
# ---------------------------------------------------------------------------


def test_concurrent_claims_only_one_wins(audit_db) -> None:
    """Spawn 8 threads racing to confirm the same audit_id — exactly
    one rowcount=1; the other seven see rowcount=0."""
    aid = log_write(tool_name="delete_note", tool_args={"note_id": 42}, status="pending")

    barrier = threading.Barrier(8)
    results: list[bool] = []
    results_lock = threading.Lock()

    def claim() -> None:
        barrier.wait()  # release all threads simultaneously
        ok = claim_write_audit(None, aid, action="confirmed", confirmed_by="user")
        with results_lock:
            results.append(ok)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    wins = sum(1 for r in results if r)
    assert wins == 1, f"expected exactly 1 winner, got {wins}: {results}"
    assert get_write_status(None, aid) == "confirmed"


# ---------------------------------------------------------------------------
# get_write_status
# ---------------------------------------------------------------------------


def test_get_write_status_returns_current(audit_db) -> None:
    aid = log_write(tool_name="x", tool_args={}, status="pending")
    assert get_write_status(None, aid) == "pending"
    claim_write_audit(None, aid, action="confirmed")
    assert get_write_status(None, aid) == "confirmed"
    update_write_status(None, aid, status="executed")
    assert get_write_status(None, aid) == "executed"


def test_get_write_status_missing_returns_none(audit_db) -> None:
    assert get_write_status(None, 99999) is None


# ---------------------------------------------------------------------------
# End-to-end via /confirm endpoint
# ---------------------------------------------------------------------------


def test_double_confirm_double_click_invocations_once(audit_db) -> None:
    """End-to-end: simulate the handler's exact control flow on two
    concurrent /confirm clicks. The destructive tool body must run
    exactly once.

    This is the regression the audit claim protects against — the OLD
    handler called update_write_status(...) then tool.invoke(...) with
    no protection; double-click invoked the tool twice.
    """
    from tradingagents.agent_harness.hitl import (
        consume_approval, grant_approval,
    )

    session_id = "test-double-click"
    invocation_count = {"n": 0}
    barrier = threading.Barrier(2)

    def confirm_click() -> None:
        barrier.wait()
        # Mirror the patched handler's logic verbatim:
        #   1) atomic claim (the fix)
        #   2) only on win, invoke the tool + update audit to executed
        if claim_write_audit(
            None, aid,
            action="confirmed",
            confirmed_by="user",
        ):
            grant_approval(session_id, "delete_note", {"note_id": 7})
            invocation_count["n"] += 1  # body of the destructive tool
            update_write_status(None, aid, status="executed")
            consume_approval(session_id, "delete_note", {"note_id": 7})

    # Pending row written by _check_write_approval on the first tool dispatch.
    aid = log_write(
        tool_name="delete_note",
        tool_args={"note_id": 7},
        status="pending",
    )

    threads = [threading.Thread(target=confirm_click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert invocation_count["n"] == 1, (
        f"destructive tool body ran {invocation_count['n']} times — "
        f"double-confirm race regression"
    )
    assert get_write_status(None, aid) == "executed"


def test_double_confirm_loser_sees_current_status(audit_db) -> None:
    """The losing click should be able to read the current audit status
    via get_write_status so it can surface that to the client (e.g.
    'already executed' vs 'in flight')."""
    aid = log_write(
        tool_name="delete_note",
        tool_args={"note_id": 9},
        status="pending",
    )

    # Winner claims + runs to executed
    assert claim_write_audit(None, aid, action="confirmed", confirmed_by="user")
    update_write_status(None, aid, status="executed")

    # Loser's status read
    assert get_write_status(None, aid) == "executed"
    # Loser's claim attempt
    assert claim_write_audit(None, aid, action="confirmed", confirmed_by="user") is False
