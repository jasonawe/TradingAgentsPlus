"""Step 28 — cross-session fork via L3 references.

Forking = creating a new session that inherits the L3 discussion
record of an old one so the synthesizer LLM has the prior context.

The endpoint:
- POST /api/harness/sessions/fork
- body: {source_session_id, user_id?}
- returns: {session_id, inherited_from}

The orchestrator's ``_build_l3_reference_block`` (Step 21) already
reads ``discussions:{session_id}`` entries from L3 and surfaces
them in the synthesize prompt — fork just guarantees the new
session gets a fresh ``discussions:{new_sid}`` row with the same
payload so the first synthesize call has something to surface.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_fork_copies_l3_reference():
    """Forking copies the source session's discussions entry to the
    new session_id, so the next synthesize can read it."""
    from tradingagents.agent_harness.memory.l3_references import (
        AgentReferencesMemory,
    )
    from tradingagents.agent_harness.core.l3_fork import fork_session_reference
    with tempfile.TemporaryDirectory() as td:
        l3 = AgentReferencesMemory(db_path=Path(td) / "refs.sqlite")
        src = "harness-old"
        dst = "harness-new"
        l3.set(
            f"discussions:{src}",
            {"symbols": ["NVDA"], "summary": "深度分析 NVDA"},
            session_id=src,
            ttl_seconds=86400,
        )
        ok = fork_session_reference(
            l3, source_session_id=src, target_session_id=dst,
        )
        assert ok is True
        # New session should now have a discussion row with the same
        # payload but a fresh session_id.
        new_entry = l3.get(f"discussions:{dst}")
        assert new_entry is not None
        assert new_entry.value["symbols"] == ["NVDA"]
        assert new_entry.value["summary"] == "深度分析 NVDA"
        assert new_entry.session_id == dst


def test_fork_missing_source_returns_false():
    """No source entry → nothing to copy → return False."""
    from tradingagents.agent_harness.memory.l3_references import (
        AgentReferencesMemory,
    )
    from tradingagents.agent_harness.core.l3_fork import fork_session_reference
    with tempfile.TemporaryDirectory() as td:
        l3 = AgentReferencesMemory(db_path=Path(td) / "refs.sqlite")
        ok = fork_session_reference(
            l3, source_session_id="nonexistent", target_session_id="x",
        )
        assert ok is False


def test_fork_payload_records_source():
    """The forked entry's value should remember where it came from
    so the synthesize prompt can name the prior conversation."""
    from tradingagents.agent_harness.memory.l3_references import (
        AgentReferencesMemory,
    )
    from tradingagents.agent_harness.core.l3_fork import fork_session_reference
    with tempfile.TemporaryDirectory() as td:
        l3 = AgentReferencesMemory(db_path=Path(td) / "refs.sqlite")
        l3.set(
            "discussions:harness-a",
            {"symbols": ["AAPL"], "summary": "讨论 AAPL"},
            session_id="harness-a",
        )
        fork_session_reference(
            l3, source_session_id="harness-a",
            target_session_id="harness-b",
        )
        entry = l3.get("discussions:harness-b")
        assert entry.value.get("forked_from") == "harness-a"
