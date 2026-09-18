"""Step 29 — checkpoint granularity + partial replay.

Before: one row per session, last write wins. ``resume()`` replays
the *full* event chain, even if the client crashed on the very last
emit.

After: composite key ``(session_id, milestone_id)``. Each milestone
becomes a row. ``resume_from(session_id, from_node)`` finds the
latest milestone at-or-before ``from_node`` and replays only those
events.

These tests cover:
- save() upserts on (session_id, milestone_id) — different milestones
  coexist for the same session
- load_latest() returns the most recent milestone
- load_at_or_before(node) returns the latest milestone whose node
  is at-or-before the requested one
- resume_from() yields the milestone events + resume_complete with
  from_node
- Legacy checkpoint rows from pre-015 migration (``legacy:singleton``)
  are still readable by load_latest()
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _tmp_sqlite():
    """Return a fresh SQLiteStore for each test."""
    from web.storage import SQLiteStore
    td = tempfile.TemporaryDirectory()
    store = SQLiteStore(path=Path(td.name) / "test.sqlite")
    # Run all migrations
    # migrations run on __init__
    return store, td


# ------------------------------------------------------------------
# Store-level granularity
# ------------------------------------------------------------------
def test_save_creates_distinct_rows_per_milestone():
    from tradingagents.agent_harness.core.harness_checkpoint import (
        HarnessCheckpoint, HarnessCheckpointStore,
        NODE_PLANNING, NODE_EXECUTING, NODE_OBSERVING,
    )
    store, td = _tmp_sqlite()
    try:
        ckpt_store = HarnessCheckpointStore(store)
        sid = "harness-m1"
        for node in (NODE_PLANNING, NODE_EXECUTING, NODE_OBSERVING):
            ckpt_store.save(HarnessCheckpoint(
                session_id=sid,
                node_position=node,
                state={"stage": node},
            ))
        milestones = ckpt_store.list_milestones(sid)
        assert len(milestones) == 3
        nodes = {m.node_position for m in milestones}
        assert nodes == {NODE_PLANNING, NODE_EXECUTING, NODE_OBSERVING}
    finally:
        td.cleanup()


def test_load_latest_returns_most_recent_milestone():
    from tradingagents.agent_harness.core.harness_checkpoint import (
        HarnessCheckpoint, HarnessCheckpointStore,
        NODE_PLANNING, NODE_EXECUTING, NODE_DONE,
    )
    store, td = _tmp_sqlite()
    try:
        ckpt_store = HarnessCheckpointStore(store)
        sid = "harness-m2"
        ckpt_store.save(HarnessCheckpoint(session_id=sid, node_position=NODE_PLANNING, state={}))
        ckpt_store.save(HarnessCheckpoint(session_id=sid, node_position=NODE_EXECUTING, state={}))
        ckpt_store.save(HarnessCheckpoint(session_id=sid, node_position=NODE_DONE, state={"final": 1}))
        latest = ckpt_store.load_latest(sid)
        assert latest is not None
        assert latest.node_position == NODE_DONE
        assert latest.state["final"] == 1
    finally:
        td.cleanup()


def test_load_at_or_before_picks_latest_in_window():
    """When client crashes during synthesizing, resume_from(synthesizing)
    should pick the latest checkpoint at-or-before synthesizing
    (i.e. observing or earlier, NOT done)."""
    from tradingagents.agent_harness.core.harness_checkpoint import (
        HarnessCheckpoint, HarnessCheckpointStore,
        NODE_PLANNING, NODE_EXECUTING, NODE_OBSERVING,
        NODE_SYNTHESIZING, NODE_DONE,
    )
    store, td = _tmp_sqlite()
    try:
        ckpt_store = HarnessCheckpointStore(store)
        sid = "harness-m3"
        for node in (NODE_PLANNING, NODE_EXECUTING, NODE_OBSERVING, NODE_SYNTHESIZING, NODE_DONE):
            ckpt_store.save(HarnessCheckpoint(session_id=sid, node_position=node, state={"n": node}))
        # resume_from(synthesizing) should pick the synthesizing row, not done
        ckpt = ckpt_store.load_at_or_before(sid, NODE_SYNTHESIZING)
        assert ckpt is not None
        assert ckpt.node_position == NODE_SYNTHESIZING
        # resume_from(observing) should pick observing
        ckpt = ckpt_store.load_at_or_before(sid, NODE_OBSERVING)
        assert ckpt is not None
        assert ckpt.node_position == NODE_OBSERVING
        # resume_from(planning) should pick planning
        ckpt = ckpt_store.load_at_or_before(sid, NODE_PLANNING)
        assert ckpt is not None
        assert ckpt.node_position == NODE_PLANNING
    finally:
        td.cleanup()


def test_legacy_singleton_milestone_still_loads():
    """Pre-015 rows have milestone_id='legacy:singleton' and should be
    returned by load_latest()."""
    from tradingagents.agent_harness.core.harness_checkpoint import (
        LEGACY_MILESTONE_ID,
        HarnessCheckpoint, HarnessCheckpointStore,
        NODE_PLANNING,
    )
    store, td = _tmp_sqlite()
    try:
        ckpt_store = HarnessCheckpointStore(store)
        # Insert a legacy row directly via SQL (skip save() so we don't
        # auto-generate a counter milestone_id).
        with ckpt_store._store._connect() as conn:
            conn.execute(
                "INSERT INTO harness_checkpoints "
                "(session_id, milestone_id, state_json, node_position, "
                "emitted_events, token_usage, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("harness-legacy", LEGACY_MILESTONE_ID, "{}", NODE_PLANNING,
                 "[]", "{}", "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00"),
            )
            conn.commit()
        latest = ckpt_store.load_latest("harness-legacy")
        assert latest is not None
        assert latest.milestone_id == LEGACY_MILESTONE_ID
        assert latest.node_position == NODE_PLANNING
    finally:
        td.cleanup()


def test_delete_removes_all_milestones_for_session():
    from tradingagents.agent_harness.core.harness_checkpoint import (
        HarnessCheckpoint, HarnessCheckpointStore,
        NODE_PLANNING, NODE_EXECUTING,
    )
    store, td = _tmp_sqlite()
    try:
        ckpt_store = HarnessCheckpointStore(store)
        sid = "harness-m4"
        for node in (NODE_PLANNING, NODE_EXECUTING):
            ckpt_store.save(HarnessCheckpoint(session_id=sid, node_position=node, state={}))
        assert ckpt_store.has_checkpoint(sid)
        deleted = ckpt_store.delete(sid)
        assert deleted is True
        assert not ckpt_store.has_checkpoint(sid)
        assert ckpt_store.list_milestones(sid) == []
    finally:
        td.cleanup()


def test_save_auto_synthesises_milestone_id():
    """Caller omits milestone_id -> store assigns "<node>:<counter>".
    """
    from tradingagents.agent_harness.core.harness_checkpoint import (
        HarnessCheckpoint, HarnessCheckpointStore,
        NODE_PLANNING,
    )
    store, td = _tmp_sqlite()
    try:
        ckpt_store = HarnessCheckpointStore(store)
        sid = "harness-auto"
        # Two saves for same node → counter increments
        ckpt_store.save(HarnessCheckpoint(session_id=sid, node_position=NODE_PLANNING, state={}))
        ckpt_store.save(HarnessCheckpoint(session_id=sid, node_position=NODE_PLANNING, state={"v": 2}))
        ms = ckpt_store.list_milestones(sid)
        assert len(ms) == 2
        ids = sorted([m.milestone_id for m in ms])
        assert ids[0] == "planning:1"
        assert ids[1] == "planning:2"
    finally:
        td.cleanup()


# ------------------------------------------------------------------
# Orchestrator-level partial replay
# ------------------------------------------------------------------
def test_orchestrator_resume_from_skips_earlier_milestones():
    """Stub-style test: orchestrator with checkpoint store populated
    with three milestones. resume_from(executing) should yield events
    from the executing milestone only, not planning."""
    from tradingagents.agent_harness.core.harness_checkpoint import (
        HarnessCheckpoint, HarnessCheckpointStore,
        NODE_PLANNING, NODE_EXECUTING, NODE_DONE,
    )
    from tradingagents.agent_harness.harness import Harness
    h = Harness()
    # Manually wire a checkpoint store
    from web.storage import SQLiteStore
    td = tempfile.TemporaryDirectory()
    try:
        store = SQLiteStore(path=Path(td.name) / "orch.sqlite")
        # migrations run on __init__
        ckpt_store = HarnessCheckpointStore(store)
        h.set_checkpoint_store(ckpt_store)
        sid = "harness-orch"
        # Populate 3 milestones with non-overlapping events
        ckpt_store.save(HarnessCheckpoint(
            session_id=sid, node_position=NODE_PLANNING,
            state={"i": 1}, emitted_events=[("plan_ready", {"v": 1})],
        ))
        ckpt_store.save(HarnessCheckpoint(
            session_id=sid, node_position=NODE_EXECUTING,
            state={"i": 2}, emitted_events=[("tool_call", {"v": 2})],
        ))
        ckpt_store.save(HarnessCheckpoint(
            session_id=sid, node_position=NODE_DONE,
            state={"i": 3}, emitted_events=[("agent_final", {"v": 3})],
        ))

        async def _collect():
            events = []
            async for ev, payload in h.orchestrator.resume_from(sid, NODE_EXECUTING):
                events.append((ev, payload))
            return events

        import asyncio
        events = asyncio.run(_collect())
        # Should include tool_call + resume_complete, NOT plan_ready
        ev_names = [e[0] for e in events]
        assert "tool_call" in ev_names
        assert "plan_ready" not in ev_names
        last_ev, last_payload = events[-1]
        assert last_ev == "resume_complete"
        assert last_payload["from_node"] == NODE_EXECUTING
        assert last_payload["node_position"] == NODE_EXECUTING
        assert last_payload["events_replayed"] == 1
    finally:
        td.cleanup()


def test_orchestrator_resume_from_no_checkpoint_returns_error():
    from tradingagents.agent_harness.harness import Harness
    from web.storage import SQLiteStore
    from tradingagents.agent_harness.core.harness_checkpoint import (
        HarnessCheckpointStore,
    )
    h = Harness()
    td = tempfile.TemporaryDirectory()
    try:
        store = SQLiteStore(path=Path(td.name) / "empty.sqlite")
        # migrations run on __init__
        ckpt_store = HarnessCheckpointStore(store)
        h.set_checkpoint_store(ckpt_store)

        async def _collect():
            events = []
            async for ev, payload in h.orchestrator.resume_from("nonexistent", "executing"):
                events.append((ev, payload))
            return events

        import asyncio
        events = asyncio.run(_collect())
        assert events[0][0] == "error"
        assert "no checkpoint" in events[0][1]["error"]
    finally:
        td.cleanup()
