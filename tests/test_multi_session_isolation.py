"""Multi-session isolation — §P3-3 cross-session hardening.

The previous §P3-3 work wired entity × op dispatch (CRUD table) but
didn't address cross-session concerns. This module covers:

  1. **MemoryManager.for_session()** — session-bound view, L1 isolation,
     L2/L3 shared.
  2. **Two concurrent sessions don't trample each other** —
     orchestrator stream_chat for sid_A and sid_B write to disjoint
     L1 keys (and don't see each other's __session_ctx__).
  3. **Same session concurrent triggers busy** —
     SessionLockManager yields `busy` event on contention.
  4. **Delete session doesn't affect siblings** —
     DELETE /api/agent/sessions/{id} (or SessionStore.delete) leaves
     other sessions' L1 history intact.
  5. **L2 preferences cross-session** — same `user_id` preference
     is visible from any session.
  6. **Crash recovery per session** —
     HarnessCheckpoint for sid_A is not returned by resume(sid_B).
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock as MM

import pytest

from tradingagents.agent_harness.core.orchestrator import Orchestrator
from tradingagents.agent_harness.core.retry import (
    CircuitBreaker, RetryPolicy,
)
from tradingagents.agent_harness.core.context import ContextPriority
from tradingagents.agent_harness.core.session_lock import SessionLockManager
from tradingagents.agent_harness.core.session_store import (
    SESSION_STATUS_ACTIVE,
    Session,
    SessionStore,
)
from tradingagents.agent_harness.tools.builtin import install_builtin_tools
from tradingagents.agent_harness.tools.registry import ToolRegistry
from tradingagents.agent_harness.memory import (
    MemoryManager,
    SqliteSessionMemory,
    UserPreferencesMemory,
    AgentReferencesMemory,
    _SessionBoundManager,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memory"


@pytest.fixture
def fresh_mm(memory_dir: Path) -> MemoryManager:
    """Build a fresh MemoryManager backed by tmp_path."""
    return MemoryManager(data_dir=str(memory_dir))


# ---------------------------------------------------------------------------
# 1. MemoryManager.for_session()
# ---------------------------------------------------------------------------

class TestForSession:
    """Session-bound view of MemoryManager — L1 isolation + L2/L3 shared."""

    def test_for_session_returns_bound_view(self, fresh_mm: MemoryManager):
        view = fresh_mm.for_session("s_A")
        assert isinstance(view, _SessionBoundManager)
        assert view.session_id == "s_A"

    def test_bound_view_defaults_session_id_for_append(self, fresh_mm):
        a = fresh_mm.for_session("s_A")
        a.append_message(None, "user", "A1")
        a.append_message(None, "user", "A2")
        hist = a.get_history()
        assert [m["content"] for m in hist] == ["A1", "A2"]

    def test_l1_isolated_between_sessions(self, fresh_mm):
        a = fresh_mm.for_session("s_A")
        b = fresh_mm.for_session("s_B")
        a.append_message(None, "user", "A-only")
        b.append_message(None, "user", "B-only")
        assert [m["content"] for m in a.get_history()] == ["A-only"]
        assert [m["content"] for m in b.get_history()] == ["B-only"]

    def test_l1_uses_bound_session_id_for_set(self, fresh_mm):
        a = fresh_mm.for_session("s_A")
        a.set("last_discussed_symbol", "600036.SS", scope=fresh_mm.l1.scope)
        # The key should land under s_A, not "default"
        a_entry = a.get("last_discussed_symbol")
        assert a_entry is not None
        assert a_entry.session_id == "s_A"

    def test_explicit_session_id_overrides_bound(self, fresh_mm):
        a = fresh_mm.for_session("s_A")
        a.append_message(None, "user", "A via bound")
        a.append_message("s_other", "user", "Explicit override")
        # bound view sees only A
        assert [m["content"] for m in a.get_history()] == ["A via bound"]
        # explicit session_id sees the override entry
        assert [m["content"] for m in fresh_mm.get_history("s_other")] == [
            "Explicit override"
        ]

    def test_l2_preferences_are_shared_across_sessions(self, fresh_mm):
        a = fresh_mm.for_session("s_A")
        b = fresh_mm.for_session("s_B")
        a.l2.set("default_provider", "eastmoney", session_id="default")
        val_b = b.l2.get("default_provider", session_id="default")
        assert val_b is not None
        assert val_b.value == "eastmoney"

    def test_l3_references_are_shared_across_sessions(self, fresh_mm):
        a = fresh_mm.for_session("s_A")
        b = fresh_mm.for_session("s_B")
        a.remember_quote("600036.SS", {"price": 40.5, "ts": 1})
        recalled = b.recall_quote("600036.SS")
        assert recalled is not None
        assert recalled.value["price"] == 40.5

    def test_bound_view_does_not_share_l1_keys_with_parent(self, fresh_mm):
        # Parent writes to default session_id (no bound)
        fresh_mm.l1.append_message("default", "user", "global")
        # A is bound to s_A — must NOT see the "default" entry.
        a = fresh_mm.for_session("s_A")
        assert a.get_history() == []

    def test_rebind_returns_sibling_not_nested(self, fresh_mm):
        a = fresh_mm.for_session("s_A")
        b = a.for_session("s_B")
        assert isinstance(b, _SessionBoundManager)
        assert b.session_id == "s_B"
        assert a.session_id == "s_A"  # unchanged

    def test_for_session_idempotent_returns_fresh_views(self, fresh_mm):
        """Each call returns a new wrapper (cheap, no shared state)."""
        a1 = fresh_mm.for_session("s_A")
        a2 = fresh_mm.for_session("s_A")
        assert a1 is not a2  # different wrapper objects
        assert a1.session_id == a2.session_id == "s_A"
        # But underlying L1 storage is shared (so both views see the same data)
        a1.append_message(None, "user", "shared")
        assert [m["content"] for m in a2.get_history()] == ["shared"]

    def test_get_layer_passthrough(self, fresh_mm):
        from tradingagents.agent_harness.memory.base import MemoryScope
        a = fresh_mm.for_session("s_A")
        assert a.get_layer(MemoryScope.SESSION) is fresh_mm.l1
        assert a.get_layer(MemoryScope.PREFERENCES) is fresh_mm.l2
        assert a.get_layer(MemoryScope.REFERENCES) is fresh_mm.l3


# ---------------------------------------------------------------------------
# 2. Two concurrent sessions don't trample each other
# ---------------------------------------------------------------------------

class TestCrossSessionL1Isolation:
    """Stream-chat for sid_A and sid_B in the same MemoryManager must
    not bleed into each other's L1 keys."""

    @pytest.fixture
    def mm_with_orch(self, fresh_mm: MemoryManager, tmp_path: Path):
        """A memory manager wired into an Orchestrator + a real DB for
        repos the CRUD write tools depend on."""
        from web.storage import SQLiteStore
        from web.repositories import (
            WatchlistRepository, NoteRepository, AlertRepository,
            ScheduledJobRepository,
        )
        store = SQLiteStore(tmp_path / "settings.db")
        repos = {
            "watchlist": WatchlistRepository(store),
            "notes": NoteRepository(store),
            "alerts": AlertRepository(store),
            "scheduled_jobs": ScheduledJobRepository(store),
        }
        reg = ToolRegistry()
        install_builtin_tools(reg)
        orch = Orchestrator(
            tool_registry=reg,
            agent_registry=None,
            llm_factory=None,
            context_priority=ContextPriority(memory=fresh_mm),
            retry_policy=RetryPolicy(max_retries=1, backoff_seconds=0),
            circuit_breaker=CircuitBreaker(failure_threshold=10, reset_seconds=30),
            audit=None,
            memory=fresh_mm,
        )
        # Inject the repos so CRUD write tools (add_to_watchlist etc.)
        # can mutate.
        from tradingagents.agents.general import tools_bridge
        prev = dict(getattr(tools_bridge, "_repos", {}))
        tools_bridge._repos = {**prev, **repos}
        yield mm_with_orch_factory(orch, fresh_mm, repos)
        tools_bridge._repos = prev
        return None

    @pytest.fixture
    def _install_mock(self, monkeypatch):
        from tradingagents.data.providers import registry as reg_mod

        class _MockProvider:
            name = "mock"

            def supports(self, symbol, asset_type, capability):
                return True

            def get_quote(self, symbol, asset_type):
                from datetime import datetime, timezone
                from web.market_models import QuoteSnapshot, Freshness
                return QuoteSnapshot(
                    symbol=symbol, price=10.0, change=0.1, change_percent=1.0,
                    volume=1000, freshness=Freshness.DELAYED,
                    as_of=datetime.now(timezone.utc),
                    fetched_at=datetime.now(timezone.utc),
                )

            def get_candles(self, symbol, interval, start, end, asset_type):
                return []

            def get_identity(self, symbol, asset_type):
                from web.market_models import AssetIdentity
                return AssetIdentity(symbol=symbol, asset_type=asset_type)

        monkeypatch.setitem(reg_mod.PROVIDERS, "mock", _MockProvider())
        monkeypatch.setattr(reg_mod, "_active", "mock")

    @staticmethod
    def _collect(orch, sid, msg):
        async def _run():
            out = []
            async for ev, p in orch.stream_chat(sid, msg):
                out.append((ev, p))
            return out
        return asyncio.run(_run())

    def test_two_sessions_l1_isolated(self, fresh_mm, tmp_path, _install_mock):
        """Stream-chat for sid_A writes a user msg + assistant summary
        to L1[s_A]; sid_B writes a different pair to L1[s_B]; neither
        sees the other's content."""
        from web.storage import SQLiteStore
        from web.repositories import (
            WatchlistRepository, NoteRepository, AlertRepository,
            ScheduledJobRepository,
        )
        store = SQLiteStore(tmp_path / "settings.db")
        repos = {
            "watchlist": WatchlistRepository(store),
            "notes": NoteRepository(store),
            "alerts": AlertRepository(store),
            "scheduled_jobs": ScheduledJobRepository(store),
        }
        reg = ToolRegistry()
        install_builtin_tools(reg)
        orch = Orchestrator(
            tool_registry=reg,
            agent_registry=None,
            llm_factory=None,
            context_priority=ContextPriority(memory=fresh_mm),
            retry_policy=RetryPolicy(max_retries=1, backoff_seconds=0),
            circuit_breaker=CircuitBreaker(failure_threshold=10, reset_seconds=30),
            audit=None,
            memory=fresh_mm,
        )
        from tradingagents.agents.general import tools_bridge
        prev = dict(getattr(tools_bridge, "_repos", {}))
        tools_bridge._repos = {**prev, **repos}
        try:
            # Two independent sessions — Tier 2 messages so the
            # orchestrator's _save_turn_summary populates L1 (Tier 1
            # short-circuit bypasses it).
            ev_a = self._collect(orch, "sess_A", "分析 600036.SS 估值")
            ev_b = self._collect(orch, "sess_B", "分析 513880.SS 估值")

            # L1 history is partitioned
            msgs_a = [m["content"] for m in fresh_mm.get_history("sess_A")]
            msgs_b = [m["content"] for m in fresh_mm.get_history("sess_B")]

            # Each session has at least a user message
            assert any("600036.SS" in m for m in msgs_a), f"A missing: {msgs_a}"
            assert any("513880.SS" in m for m in msgs_b), f"B missing: {msgs_b}"
            # A doesn't see B's content
            assert not any("513880.SS" in m for m in msgs_a)
            # B doesn't see A's content
            assert not any("600036.SS" in m for m in msgs_b)

            # __session_ctx__ per turn is also partitioned
            ctx_a = fresh_mm.l2.get("__session_ctx__", session_id="sess_A")
            ctx_b = fresh_mm.l2.get("__session_ctx__", session_id="sess_B")
            assert ctx_a is not None and ctx_b is not None
            assert "600036.SS" in ctx_a.value["symbols"]
            assert "513880.SS" in ctx_b.value["symbols"]
        finally:
            tools_bridge._repos = prev


# ---------------------------------------------------------------------------
# 3. Same session concurrent triggers busy
# ---------------------------------------------------------------------------

class TestSessionLockConcurrency:
    """SessionLockManager yields `busy` when the same session_id is
    already in-flight. Different session_ids do NOT block."""

    @pytest.mark.asyncio
    async def test_concurrent_same_session_yields_busy(self):
        mgr = SessionLockManager()

        async def _slow_producer():
            await asyncio.sleep(0.05)
            yield ("ok", {"v": 1})

        # Fire two concurrent producers on the SAME session_id
        async def _collect(sid):
            out = []
            async for ev, p in mgr.run(sid, _slow_producer):
                out.append((ev, p))
            return out

        async def _orchestrate():
            t1 = asyncio.create_task(_collect("s_X"))
            await asyncio.sleep(0.005)  # let t1 grab the lock
            t2 = asyncio.create_task(_collect("s_X"))
            return await asyncio.gather(t1, t2)

        out1, out2 = await _orchestrate()
        # One of them yields the actual events, the other yields busy
        all_events = out1 + out2
        kinds = [ev for ev, _ in all_events]
        busy_count = sum(1 for ev in kinds if ev == "busy")
        ok_count = sum(1 for ev in kinds if ev == "ok")
        assert busy_count == 1, f"expected exactly 1 busy event, got {kinds}"
        assert ok_count == 1, f"expected exactly 1 ok event, got {kinds}"

    @pytest.mark.asyncio
    async def test_different_sessions_do_not_block(self):
        mgr = SessionLockManager()
        finished: list[str] = []

        async def _slow_producer(sid: str):
            await asyncio.sleep(0.02)
            yield ("ok", {"sid": sid})

        async def _run(sid: str):
            async for ev, p in mgr.run(sid, lambda sid=sid: _slow_producer(sid)):
                if ev == "ok":
                    finished.append(sid)

        t1 = asyncio.create_task(_run("s_1"))
        t2 = asyncio.create_task(_run("s_2"))
        await asyncio.gather(t1, t2)
        assert sorted(finished) == ["s_1", "s_2"]


# ---------------------------------------------------------------------------
# 4. Delete session doesn't affect siblings
# ---------------------------------------------------------------------------

class TestSessionDeleteIsolation:
    """DELETE one session must not cascade-trample other sessions' data."""

    @pytest.fixture
    def sqlite_store(self, tmp_path: Path):
        from web.storage import SQLiteStore
        return SQLiteStore(tmp_path / "sessions.db")

    def test_delete_session_cascade_clears_its_own_state(self, sqlite_store):
        from tradingagents.agent_harness.core.harness_checkpoint import (
            HarnessCheckpoint, HarnessCheckpointStore,
        )
        store = SessionStore(sqlite_store)
        ckpt_store = HarnessCheckpointStore(sqlite_store)
        store.upsert(Session(id="s_1"))
        store.upsert(Session(id="s_2"))
        # Add harness checkpoint for s_1 (the DELETE should clear this)
        ckpt_store.save(HarnessCheckpoint(
            session_id="s_1", node_position="plan_ready",
            state={"intent": "watchlist"}, emitted_events=[],
        ))

        deleted = store.delete("s_1")
        assert deleted["sessions"] == 1
        assert deleted["harness_checkpoints"] == 1

        # s_2 row is intact
        s2 = store.get("s_2")
        assert s2 is not None
        assert s2.id == "s_2"

        # s_1 row is gone
        assert store.get("s_1") is None
        assert ckpt_store.load("s_1") is None

    def test_delete_only_clears_harness_checkpoints_for_target(self, sqlite_store):
        from tradingagents.agent_harness.core.harness_checkpoint import (
            HarnessCheckpoint, HarnessCheckpointStore,
        )
        store = SessionStore(sqlite_store)
        ckpt_store = HarnessCheckpointStore(sqlite_store)
        store.upsert(Session(id="s_A"))
        store.upsert(Session(id="s_B"))
        ckpt_store.save(HarnessCheckpoint(
            session_id="s_A", node_position="plan_ready",
            state={"intent": "alert"}, emitted_events=[],
        ))
        ckpt_store.save(HarnessCheckpoint(
            session_id="s_B", node_position="plan_ready",
            state={"intent": "note"}, emitted_events=[],
        ))

        store.delete("s_A")
        # s_A checkpoint is gone
        assert ckpt_store.load("s_A") is None
        # s_B checkpoint is intact
        b = ckpt_store.load("s_B")
        assert b is not None
        assert b.state["intent"] == "note"
        assert b.session_id == "s_B"


# ---------------------------------------------------------------------------
# 5. L2 preferences cross-session (already covered in TestForSession but
#    keep an explicit end-to-end test for clarity)
# ---------------------------------------------------------------------------

class TestL2CrossSession:
    """L2 preferences keyed by ``user_id`` are visible from any session."""

    def test_preference_set_in_session_visible_in_other(
        self, fresh_mm: MemoryManager
    ):
        fresh_mm.l2.set("default_provider", "akshare", session_id="default")
        # Two bound views from different sessions
        a = fresh_mm.for_session("s_alpha")
        b = fresh_mm.for_session("s_beta")
        val_a = a.l2.get("default_provider", session_id="default")
        val_b = b.l2.get("default_provider", session_id="default")
        assert val_a.value == "akshare"
        assert val_b.value == "akshare"


# ---------------------------------------------------------------------------
# 6. Crash recovery per session
# ---------------------------------------------------------------------------

class TestCheckpointPerSession:
    """HarnessCheckpoint for sid_A is not returned by resume(sid_B)."""

    def test_checkpoint_isolated_per_session(self, tmp_path: Path):
        from tradingagents.agent_harness.core.harness_checkpoint import (
            HarnessCheckpoint, HarnessCheckpointStore,
        )
        from web.storage import SQLiteStore
        store = SQLiteStore(tmp_path / "checkpoints.db")
        ckpt_store = HarnessCheckpointStore(store)

        ckpt_store.save(HarnessCheckpoint(
            session_id="s_A", node_position="plan_ready",
            state={"intent": "watchlist"}, emitted_events=[],
        ))
        ckpt_store.save(HarnessCheckpoint(
            session_id="s_B", node_position="plan_ready",
            state={"intent": "note"}, emitted_events=[],
        ))

        # load(sid) returns the right one
        a = ckpt_store.load("s_A")
        b = ckpt_store.load("s_B")
        assert a is not None and a.session_id == "s_A"
        assert b is not None and b.session_id == "s_B"

        # No cross-contamination
        assert a.state["intent"] == "watchlist"
        assert b.state["intent"] == "note"

    def test_delete_session_clears_its_checkpoint(self, tmp_path: Path):
        from tradingagents.agent_harness.core.harness_checkpoint import (
            HarnessCheckpoint, HarnessCheckpointStore,
        )
        from tradingagents.agent_harness.core.session_store import (
            Session, SessionStore,
        )
        from web.storage import SQLiteStore
        store = SQLiteStore(tmp_path / "combined.db")
        ckpt_store = HarnessCheckpointStore(store)
        sess_store = SessionStore(store)

        ckpt_store.save(HarnessCheckpoint(
            session_id="s_X", node_position="plan_ready",
            state={"intent": "alert"}, emitted_events=[],
        ))
        sess_store.upsert(Session(id="s_X"))
        sess_store.upsert(Session(id="s_Y"))

        sess_store.delete("s_X")

        # s_X checkpoint is gone (SessionStore cascade)
        assert ckpt_store.load("s_X") is None
        # s_Y untouched (no row existed, but listing would still work)
        assert sess_store.get("s_Y") is not None
