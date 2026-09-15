"""HarnessCheckpointStore — crash recovery persistence."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tradingagents.agent_harness.core.harness_checkpoint import (
    ALL_NODES,
    HarnessCheckpoint,
    HarnessCheckpointStore,
    NODE_DONE,
    NODE_EXECUTING,
    NODE_PLANNING,
    _now_iso,
    make_session_id,
)


# --------------------------------------------------------------------------
# Test fixture: real SQLite store via web.storage (so we exercise the
# real table-migration path).
# --------------------------------------------------------------------------
@pytest.fixture
def sqlite_store(tmp_path: Path):
    from web.storage import SQLiteStore
    return SQLiteStore(tmp_path / "ckpt.db")


@pytest.fixture
def ckpt_store(sqlite_store) -> HarnessCheckpointStore:
    return HarnessCheckpointStore(sqlite_store)


# --------------------------------------------------------------------------
# HarnessCheckpoint serialisation
# --------------------------------------------------------------------------
class TestHarnessCheckpoint:
    def test_to_row_round_trips(self):
        c = HarnessCheckpoint(
            session_id="harness-abcd1234",
            node_position=NODE_PLANNING,
            state={"intent": "quote", "symbols": ["600036.SS"]},
            emitted_events=[("plan_started", {"intent": "quote"})],
            token_usage={"totals": {"total_tokens": 100}},
        )
        row = c.to_row()
        assert row["session_id"] == "harness-abcd1234"
        assert row["node_position"] == NODE_PLANNING
        # JSON-encoded fields
        assert json.loads(row["state_json"])["intent"] == "quote"
        assert json.loads(row["emitted_events"])[0][0] == "plan_started"
        assert json.loads(row["token_usage"])["totals"]["total_tokens"] == 100

    def test_from_row_round_trips(self):
        original = HarnessCheckpoint(
            session_id="harness-xyz",
            node_position=NODE_EXECUTING,
            state={"plan": [{"step": 1, "action": "get_quote"}]},
            emitted_events=[("plan_ready", {"steps": [{"step": 1}]})],
            token_usage={"by_agent": {"data_agent": {"calls": 1}}},
        )
        row = original.to_row()
        # mimic sqlite3.Row
        class _R(dict):
            def __getitem__(self, k):
                return super().__getitem__(k)
        restored = HarnessCheckpoint.from_row(_R(row))
        assert restored.session_id == "harness-xyz"
        assert restored.node_position == NODE_EXECUTING
        assert restored.state == original.state
        assert restored.emitted_events == original.emitted_events
        assert restored.token_usage == original.token_usage

    def test_from_row_with_missing_optional_fields(self):
        """Backward-compat: rows from older versions may not have all fields."""
        class _R(dict):
            def __getitem__(self, k):
                return super().__getitem__(k)
        row = _R({
            "session_id": "harness-old",
            "state_json": "{}",
            "node_position": NODE_DONE,
            # no emitted_events / token_usage
            "created_at": None,
            "updated_at": None,
        })
        c = HarnessCheckpoint.from_row(row)
        assert c.emitted_events == []
        assert c.token_usage == {}
        assert c.created_at  # populated with now


# --------------------------------------------------------------------------
# Save / load / delete
# --------------------------------------------------------------------------
class TestSaveLoadDelete:
    def test_save_then_load_round_trips(self, ckpt_store):
        c = HarnessCheckpoint(
            session_id="harness-aaaa1111",
            node_position=NODE_PLANNING,
            state={"intent": "quote", "symbols": ["X"]},
        )
        ckpt_store.save(c)
        loaded = ckpt_store.load("harness-aaaa1111")
        assert loaded is not None
        assert loaded.session_id == "harness-aaaa1111"
        assert loaded.node_position == NODE_PLANNING
        assert loaded.state == {"intent": "quote", "symbols": ["X"]}

    def test_save_overwrites_existing(self, ckpt_store):
        ckpt_store.save(HarnessCheckpoint(
            session_id="harness-bbbb",
            node_position=NODE_PLANNING,
            state={"step": "1"},
        ))
        ckpt_store.save(HarnessCheckpoint(
            session_id="harness-bbbb",
            node_position=NODE_EXECUTING,
            state={"step": "2"},
        ))
        loaded = ckpt_store.load("harness-bbbb")
        assert loaded.node_position == NODE_EXECUTING
        assert loaded.state == {"step": "2"}

    def test_save_updates_updated_at(self, ckpt_store):
        c1 = HarnessCheckpoint(session_id="harness-cccc", node_position=NODE_PLANNING, state={})
        old_updated = c1.updated_at
        ckpt_store.save(c1)
        # Simulate time passing then save again
        import time
        time.sleep(0.01)
        c2 = HarnessCheckpoint(session_id="harness-cccc", node_position=NODE_EXECUTING, state={})
        ckpt_store.save(c2)
        loaded = ckpt_store.load("harness-cccc")
        assert loaded.updated_at >= old_updated
        assert loaded.node_position == NODE_EXECUTING

    def test_load_returns_none_for_missing(self, ckpt_store):
        assert ckpt_store.load("harness-nonexistent") is None
        assert ckpt_store.has_checkpoint("harness-nonexistent") is False

    def test_delete_removes_checkpoint(self, ckpt_store):
        ckpt_store.save(HarnessCheckpoint(
            session_id="harness-dddd",
            node_position=NODE_PLANNING,
            state={},
        ))
        assert ckpt_store.has_checkpoint("harness-dddd") is True
        assert ckpt_store.delete("harness-dddd") is True
        assert ckpt_store.has_checkpoint("harness-dddd") is False
        # Deleting twice returns False
        assert ckpt_store.delete("harness-dddd") is False

    def test_save_with_emitted_events(self, ckpt_store):
        events = [
            ("plan_started", {"intent": "quote"}),
            ("plan_ready", {"steps": [{"step": 1}]}),
        ]
        c = HarnessCheckpoint(
            session_id="harness-eeee",
            node_position=NODE_EXECUTING,
            state={"plan": [{"step": 1}]},
            emitted_events=events,
        )
        ckpt_store.save(c)
        loaded = ckpt_store.load("harness-eeee")
        assert loaded.emitted_events == events


# --------------------------------------------------------------------------
# list_active
# --------------------------------------------------------------------------
class TestListActive:
    def test_empty_returns_empty(self, ckpt_store):
        assert ckpt_store.list_active() == []

    def test_newest_first(self, ckpt_store):
        import time
        for sid in ["a", "b", "c"]:
            ckpt_store.save(HarnessCheckpoint(
                session_id=f"harness-{sid}",
                node_position=NODE_PLANNING,
                state={},
            ))
            time.sleep(0.005)
        sids = ckpt_store.list_active(limit=10)
        # 'c' saved last → newest first
        assert sids[0] == "harness-c"

    def test_limit_respected(self, ckpt_store):
        for i in range(5):
            ckpt_store.save(HarnessCheckpoint(
                session_id=f"harness-{i:02d}",
                node_position=NODE_PLANNING,
                state={},
            ))
        assert len(ckpt_store.list_active(limit=3)) == 3


# --------------------------------------------------------------------------
# purge_stale
# --------------------------------------------------------------------------
class TestPurgeStale:
    def test_purges_old_checkpoints(self, ckpt_store):
        # Save one with artificially old updated_at — bypass save()'s
        # auto-update by setting the field AFTER save.
        from datetime import datetime, timezone
        c_old = HarnessCheckpoint(
            session_id="harness-stale",
            node_position=NODE_PLANNING,
            state={},
        )
        ckpt_store.save(c_old)
        # Force the row's updated_at to the year 2020
        with ckpt_store._store._connect() as conn:
            conn.execute(
                "UPDATE harness_checkpoints SET updated_at='2020-01-01T00:00:00+00:00' "
                "WHERE session_id='harness-stale'"
            )
            conn.commit()
        # And one fresh (save() set updated_at=now)
        ckpt_store.save(HarnessCheckpoint(
            session_id="harness-fresh",
            node_position=NODE_PLANNING,
            state={},
        ))
        # Use now = slightly after the fresh save so cutoff = ~23h ago
        now = datetime.now(timezone.utc)
        deleted = ckpt_store.purge_stale(now=now)
        assert deleted == 1
        assert not ckpt_store.has_checkpoint("harness-stale")
        assert ckpt_store.has_checkpoint("harness-fresh")

    def test_purge_ttl_hours_customisable(self, sqlite_store):
        store = HarnessCheckpointStore(sqlite_store, ttl_hours=1)
        c = HarnessCheckpoint(
            session_id="harness-veryfresh",
            node_position=NODE_PLANNING,
            state={},
        )
        store.save(c)
        # 1-hour TTL should not delete just-saved
        assert store.purge_stale() == 0


# --------------------------------------------------------------------------
# Module helpers
# --------------------------------------------------------------------------
class TestHelpers:
    def test_all_nodes_contains_done(self):
        assert NODE_DONE in ALL_NODES
        assert NODE_PLANNING in ALL_NODES

    def test_make_session_id_format(self):
        sid = make_session_id()
        assert sid.startswith("harness-")
        assert len(sid) == len("harness-") + 8

    def test_make_session_id_custom_prefix(self):
        sid = make_session_id(prefix="user-xyz")
        assert sid.startswith("user-xyz-")
