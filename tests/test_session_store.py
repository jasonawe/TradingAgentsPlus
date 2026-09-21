"""SessionStore — Session 元数据表(roadmap §1.3 A2/A3) — 严格按 spec。

Schema 字段:`id, user_id, created_at, last_active, message_count, status`
(spec §1.3 A2 基础字段)。
可选扩展:`title`(首条用户消息自动设置,sidebar 显示用)、
`token_total`(成本跟踪)。metadata_json 列保留供未来扩展。

历史 rows 在 ALTER 之前创建,to_row / to_dict 通过 ``.get(...)``
安全回退;不破坏现有数据。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tradingagents.agent_harness.core.session_store import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
    Session,
    SessionStore,
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def sqlite_store(tmp_path: Path):
    from web.storage import SQLiteStore
    return SQLiteStore(tmp_path / "sessions.db")


@pytest.fixture
def store(sqlite_store):
    return SessionStore(sqlite_store)


# --------------------------------------------------------------------------
# Session dataclass round-trip — 严格按 spec 字段
# --------------------------------------------------------------------------
class TestSessionDataclass:
    def test_defaults(self):
        s = Session(id="s_abc")
        assert s.user_id == "default"
        assert s.status == SESSION_STATUS_ACTIVE
        assert s.message_count == 0
        # §Harness-redesign — title + token_total 是可选扩展字段,
        # 默认 None / 0,保留 ``not hasattr`` 守护 spec 演化。
        assert hasattr(s, "title") and s.title is None
        assert hasattr(s, "token_total") and s.token_total == 0
        assert not hasattr(s, "metadata")

    def test_to_row_has_spec_plus_optional_fields(self):
        s = Session(id="s_xyz", user_id="alice", message_count=5)
        row = s.to_row()
        assert set(row.keys()) == {
            "id", "user_id", "created_at", "last_active",
            "message_count", "status",
            # §Harness-redesign optional fields:
            "title", "token_total",
        }

    def test_to_dict_has_spec_plus_optional_fields(self):
        s = Session(id="s_xyz")
        d = s.to_dict()
        assert set(d.keys()) == {
            "id", "user_id", "created_at", "last_active",
            "message_count", "status",
            "title", "token_total",
        }

    def test_round_trip(self):
        s = Session(
            id="s_xyz",
            user_id="alice",
            message_count=5,
        )
        s2 = Session.from_row(s.to_row())
        assert s2.id == s.id
        assert s2.user_id == s.user_id
        assert s2.message_count == s.message_count


# --------------------------------------------------------------------------
# upsert / touch
# --------------------------------------------------------------------------
class TestUpsertAndTouch:
    def test_upsert_creates_new_row(self, store):
        store.upsert(Session(id="s1"))
        sess = store.get("s1")
        assert sess is not None
        assert sess.id == "s1"
        assert sess.message_count == 0
        assert sess.status == SESSION_STATUS_ACTIVE

    def test_upsert_existing_updates_last_active(self, store):
        s = Session(id="s1")
        s.last_active = "2020-01-01T00:00:00+00:00"
        store.upsert(s)
        # Re-upsert with new last_active
        store.upsert(Session(id="s1"))
        sess = store.get("s1")
        assert sess.last_active != "2020-01-01T00:00:00+00:00"

    def test_touch_bumps_message_count(self, store):
        store.upsert(Session(id="s1"))
        assert store.touch("s1", message_delta=1)
        assert store.get("s1").message_count == 1
        assert store.touch("s1", message_delta=3)
        assert store.get("s1").message_count == 4

    def test_touch_skips_archived(self, store):
        store.upsert(Session(id="s1"))
        store.archive("s1")
        # touch on archived returns False, count not bumped
        assert not store.touch("s1", message_delta=1)
        assert store.get("s1").message_count == 0


# --------------------------------------------------------------------------
# list / count
# --------------------------------------------------------------------------
class TestListAndCount:
    def test_list_empty(self, store):
        assert store.list_sessions() == []
        assert store.count() == 0

    def test_list_active_only_default(self, store):
        store.upsert(Session(id="s_active"))
        store.upsert(Session(id="s_arch"))
        store.archive("s_arch")
        listed = store.list_sessions(status=SESSION_STATUS_ACTIVE)
        assert [s.id for s in listed] == ["s_active"]

    def test_list_include_archived(self, store):
        store.upsert(Session(id="s_active"))
        store.upsert(Session(id="s_arch"))
        store.archive("s_arch")
        listed = store.list_sessions(status=None)
        ids = {s.id for s in listed}
        assert ids == {"s_active", "s_arch"}

    def test_list_newest_first(self, store):
        s1 = Session(id="s_old")
        s1.last_active = "2020-01-01T00:00:00+00:00"
        store.upsert(s1)
        store.upsert(Session(id="s_new"))
        listed = store.list_sessions()
        assert listed[0].id == "s_new"
        assert listed[1].id == "s_old"

    def test_count_reflects_status(self, store):
        store.upsert(Session(id="s1"))
        store.upsert(Session(id="s2"))
        store.archive("s2")
        assert store.count() == 1
        assert store.count(status=None) == 2

    def test_list_pagination(self, store):
        for i in range(5):
            store.upsert(Session(id=f"s{i}"))
        page1 = store.list_sessions(limit=2, offset=0)
        page2 = store.list_sessions(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].id != page2[0].id


# --------------------------------------------------------------------------
# delete — spec A3 row-level cascade
# --------------------------------------------------------------------------
class TestDelete:
    def test_delete_removes_session_row(self, store):
        store.upsert(Session(id="s1"))
        out = store.delete("s1")
        assert out["sessions"] == 1
        assert store.get("s1") is None

    def test_delete_missing_returns_zero(self, store):
        out = store.delete("does_not_exist")
        assert out["sessions"] == 0

    def test_delete_cascades_harness_checkpoints(self, store):
        from tradingagents.agent_harness.core.harness_checkpoint import (
            HarnessCheckpoint,
            HarnessCheckpointStore,
            NODE_PLANNING,
        )
        store.upsert(Session(id="s_cascade"))
        ckpt_store = HarnessCheckpointStore(store._store)
        ckpt_store.save(HarnessCheckpoint(
            session_id="s_cascade",
            node_position=NODE_PLANNING,
            state={"intent": "analysis"},
            emitted_events=[],
        ))
        out = store.delete("s_cascade")
        assert out["sessions"] == 1
        assert out["harness_checkpoints"] == 1
        assert ckpt_store.load("s_cascade") is None


# --------------------------------------------------------------------------
# SettingsRepository 适配 — app.state.repositories["settings"] 实际类型
# --------------------------------------------------------------------------
class TestSettingsRepositoryBackend:
    def test_settings_repository_unwrapped(self, tmp_path):
        from web.repositories import SettingsRepository
        from web.storage import SQLiteStore

        settings = SQLiteStore(tmp_path / "rep.db")
        repo = SettingsRepository(settings)
        store = SessionStore(repo)  # 传 wrapper,不传 raw SQLiteStore
        store.upsert(Session(id="s_repo", user_id="alice"))
        assert store.get("s_repo").user_id == "alice"
        assert store.count() == 1
        # delete 也通过 wrapper 工作
        out = store.delete("s_repo")
        assert out["sessions"] == 1
        assert store.get("s_repo") is None
