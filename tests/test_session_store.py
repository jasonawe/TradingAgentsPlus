"""SessionStore — session metadata + lifecycle (roadmap A2 + A3)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tradingagents.agent_harness.core.harness_checkpoint import HarnessCheckpoint, HarnessCheckpointStore
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
# Session dataclass round-trip
# --------------------------------------------------------------------------
class TestSessionDataclass:
    def test_defaults(self):
        s = Session(id="harness-abc")
        assert s.user_id == "default"
        assert s.status == SESSION_STATUS_ACTIVE
        assert s.message_count == 0
        assert s.token_total == 0
        assert s.metadata == {}

    def test_to_row_round_trip(self):
        s = Session(
            id="harness-xyz",
            user_id="alice",
            title="Compare 600036 vs 600418",
            message_count=5,
            token_total=10000,
            metadata={"intent": "compare", "symbols": ["600036", "600418"]},
        )
        row = s.to_row()
        restored = Session.from_row(dict(row))
        assert restored.id == "harness-xyz"
        assert restored.user_id == "alice"
        assert restored.title == "Compare 600036 vs 600418"
        assert restored.message_count == 5
        assert restored.metadata == {"intent": "compare", "symbols": ["600036", "600418"]}

    def test_to_dict_api_shape(self):
        s = Session(id="harness-1", title="hello")
        d = s.to_dict()
        for key in ("id", "user_id", "title", "created_at", "last_active",
                    "message_count", "token_total", "status", "metadata"):
            assert key in d


# --------------------------------------------------------------------------
# upsert + get
# --------------------------------------------------------------------------
class TestUpsertGet:
    def test_upsert_creates_new(self, store):
        s = Session(id="harness-new")
        store.upsert(s)
        loaded = store.get("harness-new")
        assert loaded is not None
        assert loaded.id == "harness-new"
        assert loaded.status == SESSION_STATUS_ACTIVE

    def test_upsert_updates_existing(self, store):
        store.upsert(Session(id="harness-up", message_count=3))
        # Upsert again with new counters
        s = Session(id="harness-up", message_count=7)
        store.upsert(s)
        loaded = store.get("harness-up")
        assert loaded.message_count == 7

    def test_get_returns_none_for_missing(self, store):
        assert store.get("harness-nonexistent") is None

    def test_upsert_preserves_created_at(self, store):
        store.upsert(Session(id="harness-keep"))
        first = store.get("harness-keep")
        store.upsert(Session(id="harness-keep"))
        second = store.get("harness-keep")
        assert first.created_at == second.created_at


# --------------------------------------------------------------------------
# touch — counters + last_active
# --------------------------------------------------------------------------
class TestTouch:
    def test_touch_increments_message_count(self, store):
        store.upsert(Session(id="harness-touch"))
        store.touch("harness-touch", message_delta=1)
        store.touch("harness-touch", message_delta=1)
        s = store.get("harness-touch")
        assert s.message_count == 2

    def test_touch_increments_token_total(self, store):
        store.upsert(Session(id="harness-tok"))
        store.touch("harness-tok", token_delta=1500)
        store.touch("harness-tok", token_delta=800)
        assert store.get("harness-tok").token_total == 2300

    def test_touch_updates_last_active(self, store):
        store.upsert(Session(id="harness-active"))
        first_active = store.get("harness-active").last_active
        import time
        time.sleep(0.01)
        store.touch("harness-active")
        new_active = store.get("harness-active").last_active
        assert new_active > first_active

    def test_touch_returns_false_for_missing(self, store):
        assert store.touch("harness-ghost") is False

    def test_touch_skips_archived_sessions(self, store):
        store.upsert(Session(id="harness-arc"))
        store.archive("harness-arc")
        # touch on archived session returns False (no update)
        result = store.touch("harness-arc", message_delta=1)
        assert result is False
        # Count remains 0
        assert store.get("harness-arc").message_count == 0


# --------------------------------------------------------------------------
# list_sessions
# --------------------------------------------------------------------------
class TestListSessions:
    def test_list_active_newest_first(self, store):
        import time
        for sid in ["a", "b", "c"]:
            store.upsert(Session(id=f"harness-{sid}"))
            time.sleep(0.005)
        result = store.list_sessions()
        assert [s.id for s in result] == ["harness-c", "harness-b", "harness-a"]

    def test_list_excludes_archived_by_default(self, store):
        store.upsert(Session(id="harness-act"))
        store.upsert(Session(id="harness-arc"))
        store.archive("harness-arc")
        result = store.list_sessions()
        assert all(s.status == "active" for s in result)
        assert any(s.id == "harness-act" for s in result)
        assert not any(s.id == "harness-arc" for s in result)

    def test_list_includes_archived_when_status_none(self, store):
        store.upsert(Session(id="harness-arc"))
        store.archive("harness-arc")
        result = store.list_sessions(status=None)
        assert any(s.id == "harness-arc" for s in result)

    def test_list_pagination(self, store):
        for i in range(10):
            store.upsert(Session(id=f"harness-{i:02d}"))
        page1 = store.list_sessions(limit=3, offset=0)
        page2 = store.list_sessions(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert set(s.id for s in page1).isdisjoint(s.id for s in page2)

    def test_list_filter_by_user(self, store):
        store.upsert(Session(id="harness-1", user_id="alice"))
        store.upsert(Session(id="harness-2", user_id="bob"))
        alice = store.list_sessions(user_id="alice")
        assert all(s.user_id == "alice" for s in alice)
        assert any(s.id == "harness-1" for s in alice)
        assert not any(s.id == "harness-2" for s in alice)

    def test_count(self, store):
        for i in range(5):
            store.upsert(Session(id=f"harness-{i}"))
        assert store.count() == 5
        store.archive("harness-0")
        assert store.count() == 4
        assert store.count(status=None) == 5


# --------------------------------------------------------------------------
# archive + delete + cascade
# --------------------------------------------------------------------------
class TestArchiveDelete:
    def test_archive_marks_status(self, store):
        store.upsert(Session(id="harness-arc"))
        assert store.archive("harness-arc") is True
        assert store.get("harness-arc").status == SESSION_STATUS_ARCHIVED

    def test_archive_returns_false_for_missing(self, store):
        assert store.archive("harness-ghost") is False

    def test_delete_removes_session(self, store):
        store.upsert(Session(id="harness-del"))
        deleted = store.delete("harness-del")
        assert deleted["sessions"] == 1
        assert store.get("harness-del") is None

    def test_delete_cascades_harness_checkpoint(self, sqlite_store, store):
        """A3 cascade: deleting a session drops its harness checkpoint too."""
        store.upsert(Session(id="harness-cascade"))
        ckpt_store = HarnessCheckpointStore(sqlite_store)
        ckpt_store.save(HarnessCheckpoint(
            session_id="harness-cascade",
            node_position="executing",
            state={"plan": [{"step": 1}]},
        ))
        assert ckpt_store.has_checkpoint("harness-cascade") is True

        deleted = store.delete("harness-cascade")
        assert deleted["sessions"] == 1
        assert deleted["harness_checkpoints"] == 1
        assert ckpt_store.has_checkpoint("harness-cascade") is False

    def test_delete_returns_zero_counts_for_missing(self, store):
        deleted = store.delete("harness-ghost")
        assert deleted == {"sessions": 0, "harness_checkpoints": 0}

    def test_delete_does_not_affect_other_sessions(self, sqlite_store, store):
        ckpt_store = HarnessCheckpointStore(sqlite_store)
        store.upsert(Session(id="harness-keep"))
        store.upsert(Session(id="harness-drop"))
        ckpt_store.save(HarnessCheckpoint(session_id="harness-keep", node_position="executing", state={}))
        ckpt_store.save(HarnessCheckpoint(session_id="harness-drop", node_position="executing", state={}))

        store.delete("harness-drop")
        assert store.get("harness-keep") is not None
        assert store.get("harness-drop") is None
        assert ckpt_store.has_checkpoint("harness-keep") is True
        assert ckpt_store.has_checkpoint("harness-drop") is False


# --------------------------------------------------------------------------
# set_title
# --------------------------------------------------------------------------
class TestSetTitle:
    def test_set_title_updates(self, store):
        store.upsert(Session(id="harness-tt"))
        assert store.set_title("harness-tt", "My Compare Session") is True
        assert store.get("harness-tt").title == "My Compare Session"

    def test_set_title_to_none_clears(self, store):
        store.upsert(Session(id="harness-clr", title="orig"))
        store.set_title("harness-clr", None)
        assert store.get("harness-clr").title is None

    def test_set_title_returns_false_for_missing(self, store):
        assert store.set_title("harness-ghost", "x") is False
