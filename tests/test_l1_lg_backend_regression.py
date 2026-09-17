"""L1 langgraph backend regression — NameError on _lg_db_path.

Stage 2 deleted tradingagents/agents/general/memory.py (which provided
agent_session_db_path) but l1_session._lg_db_path kept referencing the
deleted function. Every L1 append_message via the langgraph backend
silently NameErrored inside _save_turn_summary's broad except — chat
history was never persisted, but the SSE stream looked fine.

Fix: reconstruct the helper locally inside l1_session.py.

This test pins the behaviour so the regression can't sneak back.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.memory.l1_session import (
    SqliteSessionMemory,
    _safe_id,
    _lg_data_dir,
)


def test_safe_id_produces_filesystem_safe_slug() -> None:
    """``agent_<session_id>`` → filesystem-safe slug (prefixed)."""
    # safe_ticker_component lowercases + restricts to filesystem-safe chars.
    # That means session_ids with uppercase / punctuation get normalised.
    sid = _safe_id("Sess_A.B")
    assert sid.startswith("agent_")
    # The actual filename we end up with is purely filesystem-safe
    import re
    assert re.match(r"^[A-Za-z0-9._-]+$", sid), f"unsafe chars in {sid!r}"


def test_safe_id_rejects_path_traversal() -> None:
    """safe_ticker_component raises on unsafe chars — that's the right
    behaviour at this boundary (caller must not pass ``../../`` etc.)."""
    import pytest
    with pytest.raises(ValueError):
        _safe_id("../../etc/passwd")


def test_lg_data_dir_creates_agent_general_subdir() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = _lg_data_dir(td)
        assert d.name == "agent_general"
        assert d.exists()


def test_lg_data_dir_idempotent_when_already_agent_general() -> None:
    with tempfile.TemporaryDirectory() as td:
        agent_dir = Path(td) / "agent_general"
        agent_dir.mkdir()
        d = _lg_data_dir(agent_dir)
        assert d == agent_dir


def test_l1_lg_backend_persists_history(tmp_path: Path) -> None:
    """The actual regression — append_message must write to the LG file."""
    m = SqliteSessionMemory(
        use_langgraph_checkpointer=True,
        data_dir=str(tmp_path),
    )
    m.append_message("s1", "user", "hello")
    m.append_message("s1", "assistant", "hi back")

    hist = m.get_history("s1")
    assert len(hist) == 2
    assert hist[0] == {"role": "user", "content": "hello", "ts": hist[0]["ts"]}
    assert hist[1]["role"] == "assistant"

    # And the LG db file actually exists on disk
    sessions_dir = _lg_data_dir(str(tmp_path)) / "sessions"
    assert sessions_dir.exists()
    files = list(sessions_dir.glob("*.db"))
    assert len(files) >= 1


def test_l1_lg_backend_isolates_per_session(tmp_path: Path) -> None:
    """Two sessions get disjoint LG db files."""
    m = SqliteSessionMemory(
        use_langgraph_checkpointer=True,
        data_dir=str(tmp_path),
    )
    m.append_message("s1", "user", "msg-A")
    m.append_message("s2", "user", "msg-B")

    a = m.get_history("s1")
    b = m.get_history("s2")
    assert [m["content"] for m in a] == ["msg-A"]
    assert [m["content"] for m in b] == ["msg-B"]
