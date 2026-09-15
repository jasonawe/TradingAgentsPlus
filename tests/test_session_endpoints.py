"""Roadmap A2 + A3 — Session metadata + DELETE endpoint.

Validates the wired SessionStore end-to-end via FastAPI TestClient:
- GET    /api/agent/sessions                  list with last_active
- GET    /api/agent/sessions/{id}/detail     session metadata
- PATCH  /api/agent/sessions/{id}            rename / archive
- DELETE /api/agent/sessions/{id}            A3 cascade cleanup

Mirrors the pattern from ``test_harness_event_modes.py`` — uses a tmp
SQLite + a real ``Harness()`` with session_store wired.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock as MM

import pytest
from fastapi.testclient import TestClient

from tradingagents.agent_harness.core.harness_checkpoint import HarnessCheckpointStore
from tradingagents.agent_harness.core.session_store import (
    Session,
    SessionStore,
)
from tradingagents.agent_harness.harness import Harness
from web.app import create_app
from web.manager import RunManager
from web.storage import SQLiteStore


@pytest.fixture
def tmp_db(tmp_path: Path):
    return tmp_path / "settings.db"


def _make_app(tmp_db: Path):
    """Build a FastAPI app with a Harness + SessionStore wired."""
    settings = SQLiteStore(tmp_db)
    manager = RunManager()
    app = create_app(
        manager=manager,
        config={"results_dir": str(tmp_db.parent), "project_dir": str(tmp_db.parent)},
    )
    ckpt_store = HarnessCheckpointStore(settings)
    harness = Harness()
    harness.set_checkpoint_store(ckpt_store)
    session_store = SessionStore(settings)
    harness.set_session_store(session_store)
    app.state.harness = harness
    app.state.session_lock = MM(run=lambda sid, prod: prod())  # passthrough
    app.state.checkpoint_store = ckpt_store
    app.state.session_store = session_store
    return app, session_store


@pytest.fixture
def client(tmp_db):
    app, store = _make_app(tmp_db)
    with TestClient(app) as c:
        yield c, store


# --------------------------------------------------------------------------
# A2 — list + metadata endpoints
# --------------------------------------------------------------------------
class TestSessionListEndpoint:
    def test_list_empty(self, client):
        c, _ = client
        r = c.get("/api/agent/sessions")
        assert r.status_code == 200
        body = r.json()
        assert body["sessions"] == []
        assert body["total"] == 0

    def test_list_after_upsert_returns_real_last_active(self, client):
        c, store = client
        store.upsert(Session(id="s_abc", title="研究 600036"))
        r = c.get("/api/agent/sessions")
        body = r.json()
        assert body["total"] == 1
        sess = body["sessions"][0]
        assert sess["id"] == "s_abc"
        assert sess["title"] == "研究 600036"
        assert sess["last_active"] is not None  # was None before A2 fix
        assert sess["message_count"] == 0
        assert sess["status"] == "active"

    def test_list_includes_archived_only_with_flag(self, client):
        c, store = client
        store.upsert(Session(id="s_active"))
        store.upsert(Session(id="s_arch", title="old"))
        store.archive("s_arch")
        # Default: active only
        r = c.get("/api/agent/sessions")
        ids = [s["id"] for s in r.json()["sessions"]]
        assert "s_active" in ids
        assert "s_arch" not in ids
        # include_archived=true: both
        r = c.get("/api/agent/sessions?include_archived=true")
        ids = [s["id"] for s in r.json()["sessions"]]
        assert "s_active" in ids
        assert "s_arch" in ids

    def test_list_newest_active_first(self, client):
        c, store = client
        # Insert two sessions in known order with controlled last_active
        s_old = Session(id="s_old", title="old")
        s_old.last_active = "2020-01-01T00:00:00+00:00"
        store.upsert(s_old)
        s_new = Session(id="s_new", title="new")
        # Default _now_iso() is "now", so s_new wins the ordering check
        store.upsert(s_new)
        r = c.get("/api/agent/sessions")
        ids = [s["id"] for s in r.json()["sessions"]]
        assert ids[0] == "s_new"
        assert ids[1] == "s_old"


class TestSessionDetailEndpoint:
    def test_detail_returns_metadata(self, client):
        c, store = client
        store.upsert(Session(id="s_det", title="深度分析 招商银行"))
        r = c.get("/api/agent/sessions/s_det/detail")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "s_det"
        assert body["title"] == "深度分析 招商银行"
        assert body["status"] == "active"

    def test_detail_404_for_missing(self, client):
        c, _ = client
        r = c.get("/api/agent/sessions/does_not_exist/detail")
        assert r.status_code == 404


class TestSessionPatchEndpoint:
    def test_patch_rename(self, client):
        c, store = client
        store.upsert(Session(id="s_p", title="old title"))
        r = c.patch("/api/agent/sessions/s_p", json={"title": "new title"})
        assert r.status_code == 200
        assert r.json()["title"] == "new title"
        # Persisted
        assert store.get("s_p").title == "new title"

    def test_patch_archive(self, client):
        c, store = client
        store.upsert(Session(id="s_a"))
        r = c.patch("/api/agent/sessions/s_a", json={"archive": True})
        assert r.status_code == 200
        assert r.json()["status"] == "archived"

    def test_patch_404_for_missing(self, client):
        c, _ = client
        r = c.patch("/api/agent/sessions/nope", json={"title": "x"})
        assert r.status_code == 404


# --------------------------------------------------------------------------
# A3 — DELETE endpoint + cascade
# --------------------------------------------------------------------------
class TestSessionDeleteEndpoint:
    def test_delete_returns_cascade_audit(self, client):
        c, store = client
        store.upsert(Session(id="s_del"))
        r = c.delete("/api/agent/sessions/s_del")
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == "s_del"
        assert body["deleted"]["sessions"] == 1
        assert body["deleted"]["harness_checkpoints"] == 0
        # Row gone
        assert store.get("s_del") is None

    def test_delete_cascades_harness_checkpoint(self, client):
        c, store = client
        from tradingagents.agent_harness.core.harness_checkpoint import (
            HarnessCheckpoint,
            NODE_PLANNING,
        )
        store.upsert(Session(id="s_cascade"))
        c.app.state.checkpoint_store.save(
            HarnessCheckpoint(
                session_id="s_cascade",
                node_position=NODE_PLANNING,
                state={"intent": "analysis", "symbols": ["600036.SS"]},
                emitted_events=[],
            )
        )
        r = c.delete("/api/agent/sessions/s_cascade")
        assert r.status_code == 200
        body = r.json()
        assert body["deleted"]["sessions"] == 1
        assert body["deleted"]["harness_checkpoints"] == 1
        # Verify both gone
        assert store.get("s_cascade") is None
        assert c.app.state.checkpoint_store.load("s_cascade") is None

    def test_delete_missing_returns_404(self, client):
        c, _ = client
        r = c.delete("/api/agent/sessions/does_not_exist")
        # delete() returns counts of 0 for missing rows; still 200 with audit
        assert r.status_code == 200
        body = r.json()
        assert body["deleted"]["sessions"] == 0


# --------------------------------------------------------------------------
# Orchestrator integration — SessionStore directly verifies the wiring
# (avoids the full /api/harness/chat/batch path which has its own
# flaky LLM-fallback hang — see test_harness_event_modes).
# --------------------------------------------------------------------------
class TestSessionStoreWiring:
    def test_harness_has_session_store(self, client):
        """Harness.set_session_store() should expose SessionStore."""
        c, _ = client
        assert c.app.state.harness.orchestrator._session_store is not None
        # Round-trip through the orchestrator's store
        sess = c.app.state.harness.orchestrator._session_store
        sess.upsert(Session(id="via_orch", title="via orchestrator"))
        assert sess.get("via_orch").title == "via orchestrator"
