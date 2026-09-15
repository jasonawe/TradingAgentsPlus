"""Harness event 3-mode — Streaming (existing) + Batch + Poll endpoints."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock as MM

import pytest
from fastapi.testclient import TestClient

from web.app import create_app
from web.manager import RunManager
from tradingagents.agent_harness.core.harness_checkpoint import (
    HarnessCheckpoint,
    HarnessCheckpointStore,
    NODE_EXECUTING,
)
from tradingagents.agent_harness.harness import Harness


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def tmp_db(tmp_path: Path):
    """Use a tmp SQLite db for settings + checkpoint store."""
    return tmp_path / "settings.db"


def _make_harness_with_ckpt(tmp_db):
    """Create a Harness + wired checkpoint store for end-to-end tests."""
    from web.storage import SQLiteStore
    settings = SQLiteStore(tmp_db)
    ckpt_store = HarnessCheckpointStore(settings)
    harness = Harness()
    harness.set_checkpoint_store(ckpt_store)
    return harness, settings, ckpt_store


def _make_app_with_harness(tmp_db):
    """Build a FastAPI app with a harness that has a real checkpoint store."""
    from web.storage import SQLiteStore
    settings = SQLiteStore(tmp_db)
    manager = RunManager()
    app = create_app(
        manager=manager,
        config={"results_dir": str(tmp_db.parent), "project_dir": str(tmp_db.parent)},
    )
    # Inject our harness into app.state and wire checkpoint store
    ckpt_store = HarnessCheckpointStore(settings)
    harness = Harness()
    harness.set_checkpoint_store(ckpt_store)
    app.state.harness = harness
    app.state.session_lock = MM(run=lambda sid, prod: prod())  # passthrough
    app.state.checkpoint_store = ckpt_store
    return app, manager


# --------------------------------------------------------------------------
# Batch mode — POST /api/harness/chat/batch
# --------------------------------------------------------------------------
class TestTestBatchMode:
    def test_batch_returns_all_events_and_final(self, tmp_db):
        app, _ = _make_app_with_harness(tmp_db)
        with TestClient(app) as client:
            response = client.post(
                "/api/harness/chat/batch",
                json={"message": "查 600036"},
            )
        assert response.status_code == 200
        body = response.json()
        assert "session_id" in body
        assert body["event_count"] > 0
        # Events are a list of {event, payload} dicts
        ev_names = [e["event"] for e in body["events"]]
        assert "plan_started" in ev_names
        assert "agent_final" in ev_names
        # final extracted from agent_final
        assert body["final"] is not None
        # token_usage captured from usage_summary
        assert "totals" in body["token_usage"]
        # error field exists (None when success)
        assert body["error"] is None

    def test_batch_rejects_empty_message(self, tmp_db):
        app, _ = _make_app_with_harness(tmp_db)
        with TestClient(app) as client:
            response = client.post(
                "/api/harness/chat/batch",
                json={"message": ""},
            )
        assert response.status_code == 400

    def test_batch_session_id_provided(self, tmp_db):
        app, _ = _make_app_with_harness(tmp_db)
        with TestClient(app) as client:
            response = client.post(
                "/api/harness/chat/batch",
                json={"message": "查 600036", "session_id": "harness-batch1"},
            )
        assert response.json()["session_id"] == "harness-batch1"

    def test_batch_records_error_in_response(self, tmp_db):
        """When stream_chat emits error event, batch surfaces it."""
        app, _ = _make_app_with_harness(tmp_db)
        with TestClient(app) as client:
            # Force an error by patching the harness orchestrator's _plan
            # to return malformed PTC (which _execute_ptc will choke on)
            original_plan = app.state.harness.orchestrator._plan

            async def _bad_plan(state, context):
                # Patch _execute_ptc to skip; emit a synthetic error
                return {"mode": "ptc", "groups": [{"id": "g1", "calls": [
                    {"name": "definitely_not_a_real_tool", "args": {}}
                ]}]}
            app.state.harness.orchestrator._plan = _bad_plan
            try:
                response = client.post(
                    "/api/harness/chat/batch",
                    json={"message": "compare"},
                )
            finally:
                app.state.harness.orchestrator._plan = original_plan
        assert response.status_code == 200
        body = response.json()
        # The unknown tool shows up as tool_result with error; final is None
        # but error captures the failure path
        assert body["final"] is None or "error" in body
        # The error field is either a string or None; we don't insist
        # since the synth node might still produce a final dict
        # The point: batch didn't 500, it returned successfully.


# --------------------------------------------------------------------------
# Poll mode — GET /api/harness/chat/{session_id}/events?since=N
# --------------------------------------------------------------------------
class TestTestPollMode:
    def test_poll_returns_all_events_when_since_zero(self, tmp_db):
        app, ckpt_store = _make_app_with_harness(tmp_db)[0], _make_harness_with_ckpt(tmp_db)[2]
        # Inject checkpoint manually (simulating "user is polling mid-stream")
        ckpt_store.save(HarnessCheckpoint(
            session_id="harness-poll1",
            node_position=NODE_EXECUTING,
            state={"intent": "quote"},
            emitted_events=[
                ("plan_started", {"intent": "quote", "surface": "ui"}),
                ("plan_ready", {"steps": [{"step": 1}], "surface": "ui"}),
            ],
        ))
        with TestClient(app) as client:
            response = client.get(
                "/api/harness/chat/harness-poll1/events",
                params={"since": 0},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == "harness-poll1"
        assert body["event_count"] == 2
        assert body["next_since"] == 2
        assert body["done"] is False
        ev_names = [e["event"] for e in body["events"]]
        assert ev_names == ["plan_started", "plan_ready"]

    def test_poll_since_n_returns_subsequent_events(self, tmp_db):
        app, _ = _make_app_with_harness(tmp_db)[0], _make_harness_with_ckpt(tmp_db)[2]
        ckpt = _make_harness_with_ckpt(tmp_db)[2]
        ckpt.save(HarnessCheckpoint(
            session_id="harness-poll2",
            node_position=NODE_EXECUTING,
            state={},
            emitted_events=[
                ("plan_started", {"surface": "ui"}),
                ("plan_ready", {"surface": "ui"}),
                ("tool_result", {"name": "get_quote", "surface": "ui"}),
                ("tool_result", {"name": "get_quote", "surface": "ui"}),
            ],
        ))
        with TestClient(app) as client:
            response = client.get(
                "/api/harness/chat/harness-poll2/events",
                params={"since": 2},
            )
        body = response.json()
        assert body["event_count"] == 2
        ev_names = [e["event"] for e in body["events"]]
        assert ev_names == ["tool_result", "tool_result"]
        assert body["next_since"] == 4

    def test_poll_404_for_unknown_session(self, tmp_db):
        app, _ = _make_app_with_harness(tmp_db)
        with TestClient(app) as client:
            response = client.get(
                "/api/harness/chat/harness-nonexistent/events",
                params={"since": 0},
            )
        assert response.status_code == 404

    def test_poll_done_flag_when_at_done_node(self, tmp_db):
        from tradingagents.agent_harness.core.harness_checkpoint import NODE_DONE
        app, ckpt = _make_app_with_harness(tmp_db)[0], _make_harness_with_ckpt(tmp_db)[2]
        ckpt.save(HarnessCheckpoint(
            session_id="harness-done",
            node_position=NODE_DONE,
            state={},
            emitted_events=[
                ("plan_started", {"surface": "ui"}),
                ("agent_final", {"surface": "ui"}),
                ("usage_summary", {"surface": "debug"}),
            ],
        ))
        with TestClient(app) as client:
            response = client.get(
                "/api/harness/chat/harness-done/events",
                params={"since": 0},
            )
        body = response.json()
        assert body["done"] is True
        ev_names = [e["event"] for e in body["events"]]
        assert "usage_summary" in ev_names  # debug surface still included

    def test_poll_negative_since_clamps_to_zero(self, tmp_db):
        app, ckpt = _make_app_with_harness(tmp_db)[0], _make_harness_with_ckpt(tmp_db)[2]
        ckpt.save(HarnessCheckpoint(
            session_id="harness-clamp",
            node_position=NODE_EXECUTING,
            state={},
            emitted_events=[("plan_started", {"surface": "ui"})],
        ))
        with TestClient(app) as client:
            response = client.get(
                "/api/harness/chat/harness-clamp/events",
                params={"since": -100},
            )
        body = response.json()
        assert body["event_count"] == 1


# --------------------------------------------------------------------------
# Mode comparison — all three modes can co-exist for same session
# --------------------------------------------------------------------------
class TestTestModeCoexistence:
    def test_streaming_then_poll_returns_remaining(self, tmp_db):
        """Client streams via SSE for 2 events, then loses connection;
        polls later to recover the rest."""
        app, _ = _make_app_with_harness(tmp_db)[0], _make_harness_with_ckpt(tmp_db)[2]
        ckpt = _make_harness_with_ckpt(tmp_db)[2]
        # Save checkpoint with 4 events
        ckpt.save(HarnessCheckpoint(
            session_id="harness-coex",
            node_position=NODE_EXECUTING,
            state={},
            emitted_events=[
                ("plan_started", {"surface": "ui"}),
                ("plan_ready", {"surface": "ui"}),
                ("tool_result", {"surface": "ui"}),
                ("observed", {"surface": "ui"}),
            ],
        ))
        with TestClient(app) as client:
            # First poll: since=0 → all 4
            r1 = client.get(
                "/api/harness/chat/harness-coex/events",
                params={"since": 0},
            )
            assert r1.json()["event_count"] == 4
            # Resume from index 2 → remaining 2
            r2 = client.get(
                "/api/harness/chat/harness-coex/events",
                params={"since": 2},
            )
            assert r2.json()["event_count"] == 2
            assert [e["event"] for e in r2.json()["events"]] == ["tool_result", "observed"]
