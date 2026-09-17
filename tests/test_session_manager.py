"""SessionManager facade — single-source DELETE for session-scoped state.

Pins the contract:
  - delete() cleans sessions + harness_checkpoints + event_log + L1 LG
    file in one call.
  - Missing collaborators are silently skipped (partial wiring).
  - Returns counts per source so callers can audit.

Plus a regression test for the L1 langgraph NameError fix from
test_l1_lg_backend_regression (we import the helper here so the
SessionManager.delete path is also exercised end-to-end).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest

from tradingagents.agent_harness.core.session_manager import SessionManager
from tradingagents.agent_harness.core.session_store import (
    SESSION_STATUS_ACTIVE, Session, SessionStore,
)
from tradingagents.agent_harness.core.harness_checkpoint import (
    HarnessCheckpoint, HarnessCheckpointStore,
)
from tradingagents.agent_harness.memory import EventLog
from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_store(tmp_path: Path):
    """Tiny SQLiteStore stub backed by a single in-memory file."""
    from web.storage import SQLiteStore  # type: ignore
    p = tmp_path / "web_runs.sqlite3"
    s = SQLiteStore(str(p))
    return s


@pytest.fixture
def session_store(sqlite_store):
    return SessionStore(sqlite_store)


@pytest.fixture
def checkpoint_store(sqlite_store):
    return HarnessCheckpointStore(sqlite_store)


@pytest.fixture
def event_log(tmp_path: Path):
    return EventLog(db_path=str(tmp_path / "event_log.sqlite"))


@pytest.fixture
def l1(tmp_path: Path):
    return SqliteSessionMemory(
        use_langgraph_checkpointer=True,
        data_dir=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_partial_wiring_allowed(tmp_path: Path) -> None:
    """SessionManager can be constructed with any subset of collaborators."""
    el = EventLog(db_path=str(tmp_path / "el.sqlite"))
    mgr = SessionManager(event_log=el)
    assert mgr.session_store is None
    assert mgr.checkpoint_store is None
    assert mgr.l1 is None
    assert mgr.event_log is el


# ---------------------------------------------------------------------------
# delete — cascade
# ---------------------------------------------------------------------------


def test_delete_clears_all_four_stores(
    sqlite_store, session_store, checkpoint_store, event_log, l1,
) -> None:
    """The headline behaviour — one delete() removes everything."""
    # Seed session_store + checkpoint_store
    session_store.upsert(Session(id="s_X"))
    session_store.upsert(Session(id="s_Y"))
    checkpoint_store.save(HarnessCheckpoint(
        session_id="s_X", node_position="plan_ready",
        state={"intent": "watchlist"}, emitted_events=[],
    ))

    # Seed event_log (mix of replaceable + audit-only rows)
    event_log.append("s_X", "user/message", {"role": "user", "content": "hi"})
    event_log.append("s_X", "tool/result", {"tool": "get_quote"})
    event_log.append("s_X", "audit/tier2_complete", {"plan_steps": 1})
    # s_Y row should NOT be deleted
    event_log.append("s_Y", "user/message", {"role": "user", "content": "y"})

    # Seed L1 LG backend (per-session file)
    l1.append_message("s_X", "user", "msg-A")
    l1.append_message("s_X", "assistant", "msg-B")

    mgr = SessionManager(
        session_store=session_store,
        checkpoint_store=checkpoint_store,
        event_log=event_log,
        l1=l1,
    )
    counts = mgr.delete("s_X")

    # session + checkpoint rows cleared
    assert counts["sessions"] == 1
    assert counts["harness_checkpoints"] == 1
    # event_log: 3 rows for s_X
    assert counts["event_log_rows"] == 3
    # L1 LG file removed
    assert counts["l1_lg_files"] == 1

    # s_X metadata gone, s_Y intact
    assert session_store.get("s_X") is None
    assert session_store.get("s_Y") is not None
    # event_log: only s_Y rows remain
    remaining = event_log.events("s_Y")
    assert len(remaining) == 1
    remaining_x = event_log.events("s_X")
    assert len(remaining_x) == 0


def test_delete_with_no_collaborators_is_noop() -> None:
    """delete() with everything None is a no-op (returns zero counts)."""
    mgr = SessionManager()
    counts = mgr.delete("nothing")
    assert counts == {
        "sessions": 0,
        "harness_checkpoints": 0,
        "event_log_rows": 0,
        "l1_lg_files": 0,
    }


def test_delete_handles_missing_optional_collaborators(
    session_store, event_log,
) -> None:
    """delete() doesn't blow up when l1 / checkpoint_store are absent."""
    session_store.upsert(Session(id="s_A"))
    event_log.append("s_A", "user/message", {"role": "user"})

    mgr = SessionManager(
        session_store=session_store,
        event_log=event_log,
        # checkpoint_store + l1 absent
    )
    counts = mgr.delete("s_A")
    assert counts["sessions"] == 1
    assert counts["event_log_rows"] == 1
    assert counts["harness_checkpoints"] == 0
    assert counts["l1_lg_files"] == 0


# ---------------------------------------------------------------------------
# Read / write passthrough
# ---------------------------------------------------------------------------


def test_get_passes_through_to_session_store(session_store) -> None:
    session_store.upsert(Session(id="r_1"))
    mgr = SessionManager(session_store=session_store)
    assert mgr.get("r_1") is not None
    assert mgr.get("r_1")["id"] == "r_1"
    assert mgr.get("nonexistent") is None


def test_list_sessions_passes_through(session_store) -> None:
    session_store.upsert(Session(id="l_1"))
    session_store.upsert(Session(id="l_2"))
    mgr = SessionManager(session_store=session_store)
    rows = mgr.list_sessions(status=SESSION_STATUS_ACTIVE)
    assert {r["id"] for r in rows} == {"l_1", "l_2"}
