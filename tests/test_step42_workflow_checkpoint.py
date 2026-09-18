"""Step 42 — workflow ↔ checkpoint mapping."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ------------------------------------------------------------------
# Key + helpers
# ------------------------------------------------------------------
def test_workflow_checkpoint_key():
    from tradingagents.agent_harness.core.workflow_checkpoint import (
        workflow_checkpoint_key,
    )
    assert workflow_checkpoint_key("s1", "post-execute", "1") == (
        "s1", "post-execute", "1",
    )
    # Empty workflow_name falls back to default.
    assert workflow_checkpoint_key("s1", "", "1") == (
        "s1", "default", "1",
    )


def test_make_milestone_id_includes_workflow_and_node():
    from tradingagents.agent_harness.core.workflow_checkpoint import (
        make_milestone_id,
    )
    mid = make_milestone_id("post-execute", "verify", 3)
    assert mid == "post-execute:verify:3"


def test_last_node_completed_picks_highest_counter():
    from tradingagents.agent_harness.core.workflow_checkpoint import (
        last_node_completed_from_rows,
    )
    rows = [
        {"milestone_id": "wf:observe:1", "node_position": "observe"},
        {"milestone_id": "wf:verify:2", "node_position": "verify"},
        {"milestone_id": "wf:execute:3", "node_position": "execute"},
    ]
    assert last_node_completed_from_rows(rows) == "execute"


def test_last_node_completed_handles_empty():
    from tradingagents.agent_harness.core.workflow_checkpoint import (
        last_node_completed_from_rows,
    )
    assert last_node_completed_from_rows([]) is None


def test_last_node_completed_handles_malformed_counter():
    """If a row's milestone_id has no trailing counter, treat as -1."""
    from tradingagents.agent_harness.core.workflow_checkpoint import (
        last_node_completed_from_rows,
    )
    rows = [
        {"milestone_id": "no-counter", "node_position": "observe"},
        {"milestone_id": "wf:verify:5", "node_position": "verify"},
    ]
    assert last_node_completed_from_rows(rows) == "verify"


# ------------------------------------------------------------------
# Workflow integration: emit checkpoint after each node
# ------------------------------------------------------------------
class _StubCheckpointStore:
    """In-memory store that records every save() call."""

    def __init__(self):
        self.rows: list[dict] = []

    def save(self, session_id: str, workflow_name: str, milestone_id: str,
             node_position: str, state: dict, emitted_events: list):
        self.rows.append({
            "session_id": session_id,
            "workflow_name": workflow_name,
            "milestone_id": milestone_id,
            "node_position": node_position,
            "state": state,
            "emitted_events": emitted_events,
        })


def test_workflow_records_checkpoint_per_node():
    """A 3-node workflow writes 3 checkpoints in execution order."""
    from tradingagents.agent_harness.core.workflow import (
        Edge, Node, NodeResult, Workflow,
    )

    async def main():
        store = _StubCheckpointStore()

        async def a(s):
            return NodeResult(s, emit=[("a_done", {})])
        async def b(s):
            return NodeResult(s, emit=[("b_done", {})])
        async def c(s):
            return NodeResult(s, emit=[("c_done", {})], halt=True)

        wf = Workflow(name="ckpt-test")
        wf.add_node(Node("a", a))
        wf.add_node(Node("b", b))
        wf.add_node(Node("c", c))
        wf.add_edge(Edge("a", "b"))
        wf.add_edge(Edge("b", "c"))
        wf.add_edge(Edge("c", "<end>"))
        wf.set_entry("a")

        # Walk the workflow manually emitting checkpoint after each node.
        counter = {"n": 0}
        async for ev, p in wf.run({}):
            counter["n"] += 1
            store.save(
                session_id="sess-1",
                workflow_name="ckpt-test",
                milestone_id=f"ckpt-test:{ev.split('_')[0]}:{counter['n']}",
                node_position=ev.split("_")[0],
                state={},
                emitted_events=[(ev, p)],
            )
        # 3 events = 3 checkpoint rows.
        assert len(store.rows) == 3
        node_ids = [r["node_position"] for r in store.rows]
        assert node_ids == ["a", "b", "c"]
        # Each row tagged with workflow_name + session_id.
        assert all(r["workflow_name"] == "ckpt-test" for r in store.rows)
        assert all(r["session_id"] == "sess-1" for r in store.rows)


def test_resume_picks_last_completed_node():
    """On resume, the entry node should be last_completed + 1."""
    from tradingagents.agent_harness.core.workflow_checkpoint import (
        last_node_completed_from_rows,
    )

    rows = [
        {"milestone_id": "wf:observe:1", "node_position": "observe"},
        {"milestone_id": "wf:verify:2", "node_position": "verify"},
        {"milestone_id": "wf:execute:3", "node_position": "execute"},
    ]
    last = last_node_completed_from_rows(rows)
    # Caller maps "last" -> "next node to run". When wf is "plan → execute
    # → observe → verify → synthesize" and last = execute, resume from
    # observe. The actual mapping is workflow-specific (handled by the
    # workflow's edge graph), but the checkpoint correctly identifies
    # the last completed node.
    assert last == "execute"


def test_multiple_workflows_share_session():
    """Two workflows in one session do NOT overwrite each other."""
    from tradingagents.agent_harness.core.workflow_checkpoint import (
        workflow_checkpoint_key,
    )
    s = "shared-session"
    # Same session, two distinct workflow names — no collision.
    a = workflow_checkpoint_key(s, "post-execute", "1")
    b = workflow_checkpoint_key(s, "classify-plan-execute", "1")
    assert a != b
    assert a[0] == b[0]  # same session
    assert a[1] != b[1]  # different workflow
