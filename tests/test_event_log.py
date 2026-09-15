"""W3-D5 R1: L1 append-only event log."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.memory import Event, EventLog


@pytest.fixture
def log(tmp_path) -> EventLog:
    return EventLog(db_path=str(tmp_path / "event_log.sqlite"))


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def test_append_returns_event_with_seq(log: EventLog) -> None:
    ev = log.append("s1", "message", {"role": "user", "content": "hi"})
    assert isinstance(ev, Event)
    assert ev.seq == 1
    assert ev.session_id == "s1"
    assert ev.type == "message"
    assert ev.data == {"role": "user", "content": "hi"}
    assert ev.time > 0


def test_seq_increments_per_session(log: EventLog) -> None:
    a = log.append("s1", "message", {"i": 1})
    b = log.append("s1", "message", {"i": 2})
    c = log.append("s1", "tool", {"i": 3})
    assert (a.seq, b.seq, c.seq) == (1, 2, 3)


def test_seq_per_session_independent(log: EventLog) -> None:
    a = log.append("s1", "x", 1)
    b = log.append("s2", "x", 1)
    c = log.append("s1", "x", 2)
    assert a.seq == 1
    assert b.seq == 1
    assert c.seq == 2


def test_append_accepts_pinned_timestamp(log: EventLog) -> None:
    ev = log.append("s1", "x", 1, ts=12345.0)
    assert ev.time == 12345.0


def test_append_rejects_empty_session_id(log: EventLog) -> None:
    with pytest.raises(ValueError, match="session_id"):
        log.append("", "x", 1)


def test_append_rejects_empty_type(log: EventLog) -> None:
    with pytest.raises(ValueError, match="type"):
        log.append("s1", "", 1)


def test_concurrent_appends_get_unique_seqs(log: EventLog) -> None:
    """20 threads x 1 append each → 20 unique seqs, no collisions."""
    N = 20

    def worker(i: int) -> None:
        log.append("s", "message", {"i": i})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert log.head_seq("s") == N
    assert log.count("s") == N
    # No duplicate seqs
    seqs = [e.seq for e in log.events("s")]
    assert len(set(seqs)) == N


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def test_events_returns_in_seq_order(log: EventLog) -> None:
    for i in range(5):
        log.append("s", "x", i)
    seqs = [e.seq for e in log.events("s")]
    assert seqs == [1, 2, 3, 4, 5]


def test_events_after_seq_filters(log: EventLog) -> None:
    for i in range(5):
        log.append("s", "x", i)
    out = log.events("s", after_seq=3)
    assert [e.seq for e in out] == [4, 5]


def test_events_type_filter(log: EventLog) -> None:
    log.append("s", "message", {"role": "user"})
    log.append("s", "tool", {"name": "x"})
    log.append("s", "message", {"role": "assistant"})
    msgs = log.events("s", type_filter="message")
    assert [e.data["role"] for e in msgs] == ["user", "assistant"]


def test_events_limit(log: EventLog) -> None:
    for i in range(10):
        log.append("s", "x", i)
    out = log.events("s", limit=3)
    assert [e.seq for e in out] == [1, 2, 3]


def test_get_returns_event(log: EventLog) -> None:
    log.append("s", "x", {"k": "v"})
    ev = log.get("s", 1)
    assert ev is not None
    assert ev.data == {"k": "v"}


def test_get_missing_returns_none(log: EventLog) -> None:
    assert log.get("s", 999) is None


# ---------------------------------------------------------------------------
# Derived views (LLM chat history)
# ---------------------------------------------------------------------------


def test_messages_filters_message_type(log: EventLog) -> None:
    log.append("s", "message", {"role": "user", "content": "hi"})
    log.append("s", "tool", {"name": "x"})
    log.append("s", "message", {"role": "assistant", "content": "hello"})
    msgs = log.messages("s")
    assert [m.data["role"] for m in msgs] == ["user", "assistant"]


def test_chat_history_projects_to_llm_shape(log: EventLog) -> None:
    # W3-D6 R2: surface types are now user/message / assistant/message
    # (the old message type is still accepted by messages() but
    # doesn't flow into the surface-derived chat_history).
    log.append("s", "user/message", {"role": "user", "content": "hi"}, ts=1.0)
    log.append("s", "assistant/message", {"role": "assistant", "content": "hello"}, ts=2.0)
    hist = log.chat_history("s")
    assert hist == [
        {"role": "user", "content": "hi", "ts": 1.0, "seq": 1, "surface": "user"},
        {"role": "assistant", "content": "hello", "ts": 2.0, "seq": 2, "surface": "assistant"},
    ]


def test_chat_history_empty(log: EventLog) -> None:
    assert log.chat_history("nope") == []


# ---------------------------------------------------------------------------
# Replace (audit-friendly mutation)
# ---------------------------------------------------------------------------


def test_replace_appends_audit_event(log: EventLog) -> None:
    log.append("s", "message", {"role": "user", "content": "hi"}, ts=1.0)
    log.append("s", "message", {"role": "assistant", "content": "hello"}, ts=2.0)
    r = log.replace("s", 2, {"role": "assistant", "content": "redacted"}, reason="pii")
    assert r.type == "replace"
    assert r.data["target_seq"] == 2
    assert r.data["new_data"] == {"role": "assistant", "content": "redacted"}
    assert r.data["reason"] == "pii"
    # Original is intact and pointed-at
    original = log.get("s", 2)
    assert original.data == {"role": "assistant", "content": "hello"}
    assert original.replaced_by == r.seq


def test_replace_missing_raises(log: EventLog) -> None:
    with pytest.raises(KeyError, match="not found"):
        log.replace("s", 999, {})


def test_replace_does_not_mutate_original_data(log: EventLog) -> None:
    """Audit guarantee: the original row's data column is unchanged
    after replace — only a tombstone + the new ``replaced_by`` point
    at the corrected version.
    """
    log.append("s", "message", {"content": "original"}, ts=1.0)
    log.replace("s", 1, {"content": "corrected"}, reason="x")
    # Original row data unchanged
    assert log.get("s", 1).data == {"content": "original"}


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def test_head_seq_starts_at_zero(log: EventLog) -> None:
    assert log.head_seq("nope") == 0


def test_count_per_session(log: EventLog) -> None:
    log.append("s1", "x", 1)
    log.append("s1", "x", 2)
    log.append("s2", "x", 1)
    assert log.count("s1") == 2
    assert log.count("s2") == 1
    assert log.count() == 3


def test_clear_removes_all_for_session(log: EventLog) -> None:
    log.append("s", "x", 1)
    log.append("s", "x", 2)
    log.append("other", "x", 1)
    removed = log.clear("s")
    assert removed == 2
    assert log.count("s") == 0
    assert log.count("other") == 1


def test_len_reflects_total_rows(log: EventLog) -> None:
    log.append("s1", "x", 1)
    log.append("s1", "x", 2)
    log.append("s2", "x", 1)
    assert len(log) == 3


# ---------------------------------------------------------------------------
# Harness integration
# ---------------------------------------------------------------------------


def test_harness_has_event_log(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_DATA_DIR", str(tmp_path / "data"))
    from tradingagents.agent_harness.harness import Harness
    h = Harness()
    assert isinstance(h.event_log, EventLog)
    # Round-trip
    ev = h.event_log.append("test", "user/message", {"role": "user", "content": "hi"})
    assert ev.seq >= 1
    assert h.event_log.chat_history("test")[0]["content"] == "hi"
