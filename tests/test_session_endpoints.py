"""Roadmap §1.3 A2 + A3 — 严格按 spec 的端点测试。

Spec 端点:
- GET    /api/agent/sessions                  列出 sessions(6 字段)
- DELETE /api/agent/sessions/{id}             A3 级联清理(sessions 行 +
                                             harness_checkpoints 行 +
                                             LangGraph agent_*.db 文件)+ audit

不测 PATCH、detail、POST 增强 — spec 里没这些。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock as MM

import pytest
from fastapi.testclient import TestClient

from tradingagents.agent_harness.core.harness_checkpoint import (
    HarnessCheckpoint,
    HarnessCheckpointStore,
    NODE_PLANNING,
)
from tradingagents.agent_harness.core.session_store import Session, SessionStore
from tradingagents.agent_harness.harness import Harness
from web.app import create_app
from web.manager import RunManager
from web.storage import SQLiteStore


SPEC_FIELDS = {"id", "user_id", "created_at", "last_active",
               "message_count", "status"}


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
    harness.set_session_store(SessionStore(settings))
    app.state.harness = harness
    app.state.session_lock = MM(run=lambda sid, prod: prod())  # passthrough
    app.state.checkpoint_store = ckpt_store
    app.state.session_store = harness.orchestrator._session_store
    return app


@pytest.fixture
def client(tmp_db):
    with TestClient(_make_app(tmp_db)) as c:
        yield c


# --------------------------------------------------------------------------
# A2 — list
# --------------------------------------------------------------------------
class TestListEndpoint:
    def test_list_empty(self, client):
        r = client.get("/api/agent/sessions")
        assert r.status_code == 200
        assert r.json() == {"sessions": [], "total": 0}

    def test_list_returns_real_last_active(self, client):
        """spec §1.1.3 — 之前 stub 硬编码 last_active=None,现在必须真实。"""
        client.app.state.session_store.upsert(Session(id="s_aaa"))
        r = client.get("/api/agent/sessions")
        body = r.json()
        assert body["total"] == 1
        sess = body["sessions"][0]
        assert sess["last_active"] is not None  # was None before
        assert sess["message_count"] == 0

    def test_list_response_only_has_spec_fields(self, client):
        """spec 字段:id, user_id, created_at, last_active, message_count, status
        不带 title / token_total / metadata。"""
        client.app.state.session_store.upsert(Session(id="s_a", user_id="alice"))
        r = client.get("/api/agent/sessions")
        sess = r.json()["sessions"][0]
        assert set(sess.keys()) == SPEC_FIELDS

    def test_list_archived_filter(self, client):
        store = client.app.state.session_store
        store.upsert(Session(id="s_active"))
        store.upsert(Session(id="s_arch"))
        store.archive("s_arch")
        # Default: active only
        ids = [s["id"] for s in client.get("/api/agent/sessions").json()["sessions"]]
        assert "s_active" in ids
        assert "s_arch" not in ids
        # include_archived=true
        ids = [
            s["id"]
            for s in client.get("/api/agent/sessions?include_archived=true").json()["sessions"]
        ]
        assert "s_active" in ids and "s_arch" in ids


# --------------------------------------------------------------------------
# A3 — DELETE + 3-tier cascade + audit
# --------------------------------------------------------------------------
class TestDeleteEndpoint:
    def test_delete_returns_audit(self, client):
        client.app.state.session_store.upsert(Session(id="s_del"))
        r = client.delete("/api/agent/sessions/s_del")
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == "s_del"
        # spec A3: 级联清理 db 文件 + audit
        assert "deleted" in body
        assert "files_removed" in body
        assert body["deleted"]["sessions"] == 1
        assert client.app.state.session_store.get("s_del") is None

    def test_delete_cascades_harness_checkpoint(self, client):
        store = client.app.state.session_store
        ckpt_store = client.app.state.checkpoint_store
        store.upsert(Session(id="s_cascade"))
        ckpt_store.save(HarnessCheckpoint(
            session_id="s_cascade",
            node_position=NODE_PLANNING,
            state={"intent": "analysis"},
            emitted_events=[],
        ))
        r = client.delete("/api/agent/sessions/s_cascade")
        body = r.json()
        assert body["deleted"]["sessions"] == 1
        assert body["deleted"]["harness_checkpoints"] == 1
        assert ckpt_store.load("s_cascade") is None

    def test_delete_removes_langgraph_db_file(self, client):
        """spec A3: '级联清理 db 文件' — 必须删 LangGraph per-session db 文件。

        通过实际路径(agent_session_db_path 默认返回 Path.home()/.tradingagents)
        验证 DELETE 会删文件并把路径写入 audit payload。"""
        from pathlib import Path
        from tradingagents.agents.general.memory import agent_session_db_path

        client.app.state.session_store.upsert(Session(id="s_file_del"))
        # 预创建 LangGraph per-session db 文件
        db_file = agent_session_db_path(Path.home() / ".tradingagents", "s_file_del")
        db_file.parent.mkdir(parents=True, exist_ok=True)
        db_file.touch()
        assert db_file.exists()
        try:
            r = client.delete("/api/agent/sessions/s_file_del")
            body = r.json()
            # audit: 行级 + 文件级 都清理
            assert body["deleted"]["db_files"] == 1
            assert body["deleted"]["sessions"] == 1
            assert not db_file.exists(), f"file not removed: {db_file}"
            # audit payload 包含完整文件路径
            assert any(str(db_file) == f for f in body["files_removed"])
        finally:
            if db_file.exists():
                db_file.unlink()
