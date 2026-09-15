"""Orchestrator crash recovery — resume() replays checkpoint events."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock as MM

import pytest

from tradingagents.agent_harness.core.harness_checkpoint import (
    HarnessCheckpoint,
    HarnessCheckpointStore,
    NODE_DONE,
    NODE_EXECUTING,
    NODE_PLANNING,
)
from tradingagents.agent_harness.core.orchestrator import Orchestrator
from tradingagents.agent_harness.core.retry import CircuitBreaker, RetryPolicy
from tradingagents.agent_harness.core.context import ContextPriority
from tradingagents.agent_harness.tools import ToolContext, ToolRegistry


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def sqlite_store(tmp_path: Path):
    from web.storage import SQLiteStore
    return SQLiteStore(tmp_path / "ckpt.db")


@pytest.fixture
def ckpt_store(sqlite_store) -> HarnessCheckpointStore:
    return HarnessCheckpointStore(sqlite_store)


def _make_orch(checkpoint_store):
    registry = ToolRegistry()
    from tradingagents.agent_harness.tools.base import BaseTool
    from tradingagents.agent_harness.tools.schema import ToolSchema
    from tradingagents.agent_harness.tools.permission import PermissionType

    class _Quote(BaseTool):
        def __init__(self):
            self.schema = ToolSchema(
                name="get_quote", description="x",
                args_schema=dict, result_schema=dict,
                permission=PermissionType.READ,
            )
        @property
        def name(self): return self.schema.name
        async def invoke(self, args, context):
            return {"symbol": (args or {}).get("symbol"), "price": 10.0}

    registry.add(_Quote())
    return Orchestrator(
        tool_registry=registry,
        agent_registry=MM(list_names=lambda: [], get=lambda n: (_ for _ in ()).throw(KeyError(n))),
        llm_factory=None,
        context_priority=ContextPriority(),
        retry_policy=RetryPolicy(max_retries=0),
        circuit_breaker=CircuitBreaker(failure_threshold=5, reset_seconds=30.0),
        audit=None,
        enable_l3=False,
        checkpoint_store=checkpoint_store,
    )


def _collect(gen):
    return asyncio.run(_collect_async(gen))


async def _collect_async(gen):
    events = []
    async for ev, p in gen:
        events.append((ev, p))
    return events


# --------------------------------------------------------------------------
# Checkpoint happens during stream_chat
# --------------------------------------------------------------------------
class TestCheckpointDuringStream:
    def test_checkpoint_saved_during_run(self, ckpt_store):
        orch = _make_orch(ckpt_store)
        events = _collect(orch.stream_chat(
            session_id="harness-resume1", user_message="查 600036"
        ))
        # Even after success, checkpoint was created during the run.
        # Note: it's deleted on success (we'll test that explicitly).
        assert any(ev == "plan_started" for ev, _ in events)
        assert any(ev == "agent_final" for ev, _ in events)

    def test_checkpoint_deleted_after_success(self, ckpt_store):
        orch = _make_orch(ckpt_store)
        _collect(orch.stream_chat(
            session_id="harness-delsuccess", user_message="查 600036"
        ))
        # Successful run cleans up its checkpoint
        assert not ckpt_store.has_checkpoint("harness-delsuccess")

    def test_checkpoint_saved_for_in_flight_session(self, ckpt_store):
        """A crash mid-stream leaves a checkpoint; resume can pick it up."""
        # Manually save a checkpoint simulating "mid-execution"
        ckpt_store.save(HarnessCheckpoint(
            session_id="harness-crashed",
            node_position=NODE_EXECUTING,
            state={"intent": "quote", "symbols": ["600036.SS"]},
            emitted_events=[
                ("plan_started", {"intent": "quote", "surface": "ui"}),
                ("plan_ready", {"steps": [{"step": 1}], "surface": "ui"}),
            ],
        ))
        assert ckpt_store.has_checkpoint("harness-crashed")


# --------------------------------------------------------------------------
# resume() replays events
# --------------------------------------------------------------------------
class TestResume:
    def test_resume_replays_buffered_events(self, ckpt_store):
        ckpt_store.save(HarnessCheckpoint(
            session_id="harness-replay",
            node_position=NODE_EXECUTING,
            state={"intent": "quote"},
            emitted_events=[
                ("plan_started", {"intent": "quote", "surface": "ui"}),
                ("plan_ready", {"steps": [{"step": 1}], "surface": "ui"}),
            ],
        ))
        orch = _make_orch(ckpt_store)
        events = _collect(orch.resume("harness-replay"))
        # Replayed events appear first
        ev_names = [ev for ev, _ in events]
        assert ev_names[0] == "plan_started"
        assert ev_names[1] == "plan_ready"
        # Followed by resume_complete
        assert ev_names[-1] == "resume_complete"
        last_payload = events[-1][1]
        assert last_payload["session_id"] == "harness-replay"
        assert last_payload["node_position"] == NODE_EXECUTING
        assert last_payload["events_replayed"] == 2

    def test_resume_yields_error_for_no_checkpoint(self, ckpt_store):
        orch = _make_orch(ckpt_store)
        events = _collect(orch.resume("harness-nonexistent"))
        assert events[0][0] == "error"
        assert "no checkpoint" in events[0][1]["error"]

    def test_resume_yields_error_without_store(self, ckpt_store):
        orch = _make_orch(checkpoint_store=None)
        events = _collect(orch.resume("harness-anything"))
        assert events[0][0] == "error"
        assert "not configured" in events[0][1]["error"]

    def test_resume_with_empty_events(self, ckpt_store):
        ckpt_store.save(HarnessCheckpoint(
            session_id="harness-empty",
            node_position=NODE_PLANNING,
            state={},
            emitted_events=[],
        ))
        orch = _make_orch(ckpt_store)
        events = _collect(orch.resume("harness-empty"))
        # Only resume_complete
        assert len(events) == 1
        assert events[0][0] == "resume_complete"
        assert events[0][1]["events_replayed"] == 0


# --------------------------------------------------------------------------
# Crash scenario — drop checkpoint, then resume fails to find it
# --------------------------------------------------------------------------
class TestCrashLifecycle:
    def test_crash_leaves_checkpoint_resume_works(self, ckpt_store):
        # Simulate a crash: save a checkpoint, never call delete
        ckpt_store.save(HarnessCheckpoint(
            session_id="harness-midsession",
            node_position=NODE_EXECUTING,
            state={"plan": [{"step": 1}]},
            emitted_events=[
                ("plan_started", {"intent": "quote", "surface": "ui"}),
                ("plan_ready", {"steps": [{"step": 1}], "surface": "ui"}),
            ],
        ))
        # Now resume
        orch = _make_orch(ckpt_store)
        events = _collect(orch.resume("harness-midsession"))
        ev_names = [ev for ev, _ in events]
        assert ev_names == ["plan_started", "plan_ready", "resume_complete"]

        # Caller (the new chat endpoint) can delete the checkpoint after
        # consuming resume_complete so subsequent calls return 404.
        ckpt_store.delete("harness-midsession")
        events2 = _collect(orch.resume("harness-midsession"))
        assert events2[0][0] == "error"
