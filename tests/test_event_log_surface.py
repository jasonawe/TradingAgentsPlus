"""W3-D6 R2 + R3: Surface classification + system prompt commit."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.memory import (
    Event, EventLog, SurfaceType, TYPE_TO_SURFACE, classify_surface,
)


@pytest.fixture
def log(tmp_path) -> EventLog:
    return EventLog(db_path=str(tmp_path / "event_log.sqlite"))


# ---------------------------------------------------------------------------
# R2 — surface classification
# ---------------------------------------------------------------------------


def test_surface_type_enum() -> None:
    assert SurfaceType.SYSTEM == "system"
    assert SurfaceType.USER == "user"
    assert SurfaceType.ASSISTANT == "assistant"
    assert SurfaceType.TOOL == "tool"
    assert SurfaceType.LOG == "log"


def test_classify_known_types() -> None:
    assert classify_surface("system/message") == SurfaceType.SYSTEM
    assert classify_surface("user/message") == SurfaceType.USER
    assert classify_surface("assistant/message") == SurfaceType.ASSISTANT
    assert classify_surface("assistant/delta") == SurfaceType.ASSISTANT
    assert classify_surface("tool/result") == SurfaceType.TOOL


def test_classify_unknown_defaults_to_log() -> None:
    assert classify_surface("phase_changed") == SurfaceType.LOG
    assert classify_surface("agent_status") == SurfaceType.LOG
    assert classify_surface("replace") == SurfaceType.LOG
    assert classify_surface("totally_unknown_event") == SurfaceType.LOG


def test_event_carries_surface_on_append(log: EventLog) -> None:
    ev = log.append("s", "system/message", {"content": "you are..."})
    assert ev.surface == SurfaceType.SYSTEM
    assert ev.is_surface is True


def test_event_log_only_for_audit_types(log: EventLog) -> None:
    ev = log.append("s", "phase_changed", {"phase": "plan"})
    assert ev.surface == SurfaceType.LOG
    assert ev.is_surface is False


def test_event_to_dict_includes_surface(log: EventLog) -> None:
    ev = log.append("s", "user/message", {"role": "user", "content": "hi"})
    d = ev.to_dict()
    assert d["surface"] == "user"


# ---------------------------------------------------------------------------
# R2 — surface_events query
# ---------------------------------------------------------------------------


def test_surface_events_excludes_log_only(log: EventLog) -> None:
    log.append("s", "system/message", {"content": "sys"})
    log.append("s", "user/message", {"role": "user", "content": "hi"})
    log.append("s", "phase_changed", {"phase": "plan"})  # log-only
    log.append("s", "agent_status", {"status": "running"})  # log-only
    log.append("s", "tool/result", {"name": "get_quote"})

    surface = log.surface_events("s")
    assert [e.type for e in surface] == [
        "system/message", "user/message", "tool/result",
    ]


def test_surface_events_after_seq(log: EventLog) -> None:
    for i in range(5):
        log.append("s", "user/message", {"i": i})
    log.append("s", "phase_changed", {"phase": "x"})  # log-only, doesn't count
    out = log.surface_events("s", after_seq=3)
    assert [e.data["i"] for e in out] == [3, 4]


def test_surface_event_preserves_replaced_by(log: EventLog) -> None:
    e1 = log.append("s", "system/message", {"content": "v1"})
    log.replace("s", e1.seq, {"content": "v2"}, reason="x")
    surf = log.surface_events("s")
    assert surf[0].replaced_by == e1.seq + 1


# ---------------------------------------------------------------------------
# R3 — system prompt commit
# ---------------------------------------------------------------------------


def test_commit_system_prompt_first_time(log: EventLog) -> None:
    ev = log.commit_system_prompt("s", "You are helpful.")
    assert ev.type == "system/message"
    assert ev.surface == SurfaceType.SYSTEM
    assert ev.data["role"] == "system"
    assert ev.data["content"] == "You are helpful."


def test_commit_system_prompt_idempotent_replaces(log: EventLog) -> None:
    e1 = log.commit_system_prompt("s", "v1")
    e2 = log.commit_system_prompt("s", "v2")
    # No second system/message event — replaced via tombstone
    assert e2.type == "replace"
    # The original system event points at the replace event
    assert log.get("s", e1.seq).replaced_by == e2.seq
    # The replace event carries the new content
    assert e2.data["new_data"]["content"] == "v2"


def test_chat_history_starts_with_system(log: EventLog) -> None:
    log.commit_system_prompt("s", "system prompt here")
    log.append("s", "user/message", {"role": "user", "content": "hi"})
    hist = log.chat_history("s")
    assert hist[0]["role"] == "system"
    assert hist[0]["content"] == "system prompt here"
    assert hist[1]["role"] == "user"


def test_chat_history_full_chain(log: EventLog) -> None:
    log.commit_system_prompt("s", "you are an analyst")
    log.append("s", "user/message", {"role": "user", "content": "analyze AAPL"})
    log.append("s", "assistant/message", {"role": "assistant", "content": "P/E is 30..."})
    log.append("s", "tool/result", {"name": "get_quote", "result": {"price": 150}})
    log.append("s", "assistant/message", {"role": "assistant", "content": "P/E suggests overvalued"})

    hist = log.chat_history("s")
    assert [h["role"] for h in hist] == ["system", "user", "assistant", "tool", "assistant"]
    assert [h["surface"] for h in hist] == ["system", "user", "assistant", "tool", "assistant"]


def test_chat_history_apply_replaces_default(log: EventLog) -> None:
    log.commit_system_prompt("s", "v1")
    log.commit_system_prompt("s", "v2 (replaced)")
    hist = log.chat_history("s")
    assert hist[0]["content"] == "v2 (replaced)"


def test_chat_history_apply_replaces_false_keeps_original(log: EventLog) -> None:
    log.commit_system_prompt("s", "v1")
    log.commit_system_prompt("s", "v2 (replaced)")
    hist = log.chat_history("s", apply_replaces=False)
    assert hist[0]["content"] == "v1"


def test_chat_history_empty_session(log: EventLog) -> None:
    assert log.chat_history("never") == []


def test_chat_history_only_log_only_events(log: EventLog) -> None:
    log.append("s", "phase_changed", {"phase": "x"})
    log.append("s", "agent_status", {"status": "running"})
    # No surface events → empty history
    assert log.chat_history("s") == []


# ---------------------------------------------------------------------------
# R3 — surface-aware message / tool projection
# ---------------------------------------------------------------------------


def test_tool_result_projects_as_tool_role(log: EventLog) -> None:
    log.append("s", "tool/result", {"name": "get_quote", "content": "price=100"})
    hist = log.chat_history("s")
    assert hist[0]["role"] == "tool"
    assert hist[0]["content"] == "price=100"


def test_assistant_delta_projects_as_assistant(log: EventLog) -> None:
    log.append("s", "assistant/delta", {"content": "incremental..."})
    hist = log.chat_history("s")
    assert hist[0]["role"] == "assistant"
    assert hist[0]["content"] == "incremental..."


# ---------------------------------------------------------------------------
# Replace chain integration
# ---------------------------------------------------------------------------


def test_replace_chain_for_assistant_message(log: EventLog) -> None:
    """An assistant message can be replaced (e.g. for redaction) and
    the LLM history will project the new content.
    """
    e1 = log.append("s", "assistant/message", {"content": "original answer"})
    log.replace("s", e1.seq, {"content": "redacted answer"}, reason="pii")
    hist = log.chat_history("s")
    assert hist[0]["content"] == "redacted answer"
    # Original audit row is preserved
    original = log.get("s", e1.seq)
    assert original.data["content"] == "original answer"
    assert original.replaced_by is not None
