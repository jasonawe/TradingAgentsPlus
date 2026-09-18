"""Step 28 D+E — fork API endpoint + plan-cache web wiring smoke tests.

D — POST /api/harness/sessions/fork
    - 400 if source_session_id missing
    - 200 + new session_id + inherited_from on success
    - L3 fork only fires when source had a discussions:<src> row

E — Plan cache wiring
    - Harness.set_plan_cache_db_path swaps orchestrator.plan_cache to
      PersistentPlanCache; setting None is a no-op.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ------------------------------------------------------------------
# D — fork endpoint
# ------------------------------------------------------------------
def test_fork_endpoint_returns_new_sid():
    from fastapi.testclient import TestClient
    from web.app import create_app
    app = create_app()
    client = TestClient(app)

    r = client.post("/api/harness/sessions/fork", json={})
    assert r.status_code == 400, r.text

    r = client.post(
        "/api/harness/sessions/fork",
        json={"source_session_id": "harness-source-1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inherited_from"] == "harness-source-1"
    assert body["session_id"]
    assert body["session_id"] != "harness-source-1"
    # forked=False because source had no discussions row
    assert body["forked"] is False


def test_fork_endpoint_copies_l3_when_source_has_discussion():
    """End-to-end: write a discussions:<src> row to L3, then fork, the
    new session should inherit it."""
    from fastapi.testclient import TestClient
    from web.app import create_app
    app = create_app()
    client = TestClient(app)

    src_sid = "harness-l3-source"
    # Set discussions:<src> via the actual memory L3 layer.
    l3 = app.state.harness.memory.l3
    l3.set(
        f"discussions:{src_sid}",
        {"summary": "test summary", "topic": "AAPL"},
        session_id=src_sid,
    )

    r = client.post(
        "/api/harness/sessions/fork",
        json={"source_session_id": src_sid},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    new_sid = body["session_id"]
    assert body["forked"] is True

    # Confirm L3 has discussions:<new_sid> with the inherited payload.
    dst_entry = l3.get(f"discussions:{new_sid}")
    assert dst_entry is not None, "L3 row missing on new session"
    assert dst_entry.value.get("forked_from") == src_sid
    assert dst_entry.value.get("topic") == "AAPL"


# ------------------------------------------------------------------
# E — plan cache wiring on Harness
# ------------------------------------------------------------------
def test_set_plan_cache_db_path_swaps_to_persistent():
    from tradingagents.agent_harness.harness import Harness
    h = Harness()
    # Default is in-memory
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    assert isinstance(h.orchestrator.plan_cache, PlanTemplateCache)

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "pc.sqlite")
        h.set_plan_cache_db_path(db)
        from tradingagents.agent_harness.core.persistent_plan_cache import (
            PersistentPlanCache,
        )
        assert isinstance(h.orchestrator.plan_cache, PersistentPlanCache)
        assert str(h.orchestrator.plan_cache._db_path) == db


def test_set_plan_cache_db_path_none_is_noop():
    """Passing None must NOT crash and must leave cache as in-memory."""
    from tradingagents.agent_harness.harness import Harness
    from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
    h = Harness()
    h.set_plan_cache_db_path(None)
    assert isinstance(h.orchestrator.plan_cache, PlanTemplateCache)


def test_set_plan_cache_db_path_accepts_env_kwargs():
    """Custom TTL/max_entries propagate to PersistentPlanCache."""
    from tradingagents.agent_harness.harness import Harness
    from tradingagents.agent_harness.core.persistent_plan_cache import (
        PersistentPlanCache,
    )
    h = Harness()
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "pc.sqlite")
        h.set_plan_cache_db_path(db, ttl_seconds=42.0, max_entries=7)
        c = h.orchestrator.plan_cache
        assert isinstance(c, PersistentPlanCache)
        assert c.ttl_seconds == 42.0
        assert c.max_entries == 7
