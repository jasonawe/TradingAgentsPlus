"""Audit log → event_log consolidation.

Before: AuditLogger wrote JSONL into ``audit.log`` and nothing read it.
After: when wired with an EventLog, lifecycle events flow through the
same SQLite store as conversation events (prefix ``audit/``). JSONL is
the fallback for standalone usage.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.memory import EventLog
from tradingagents.agent_harness.observability import AuditLogger


def test_audit_logger_routes_through_event_log(tmp_path: Path) -> None:
    el = EventLog(db_path=str(tmp_path / "event_log.sqlite"))
    al = AuditLogger(data_dir=tmp_path, event_log=el)

    assert al.backend == "event_log"
    al.log("s1", "tier2_complete", {"intent": "watchlist", "plan_steps": 3})
    al.log("s1", "tier2_complete", {"intent": "notes", "plan_steps": 1})
    al.log("s2", "tier2_complete", {"intent": "analysis"})

    # Conversation events should NOT be in audit; audit events should NOT
    # be in the LLM chat history. Both are checked via the SQLite store.
    rows = el.events_by_type_prefix(None, "audit/", limit=10)
    assert len(rows) == 3
    assert all(r.type.startswith("audit/") for r in rows)
    assert {r.session_id for r in rows} == {"s1", "s2"}

    # tail() should reproduce the legacy shape (ts / session_id /
    # event / payload with the type prefix stripped).
    tail = al.tail(n=10)
    assert len(tail) == 3
    for entry in tail:
        assert set(entry.keys()) == {"ts", "session_id", "event", "payload"}
        assert not entry["event"].startswith("audit/")  # prefix stripped
    # tail orders DESC by time; most recent first
    assert tail[0]["session_id"] == "s2"


def test_audit_logger_falls_back_to_jsonl_without_event_log(
    tmp_path: Path,
) -> None:
    """Standalone AuditLogger keeps the legacy JSONL behaviour."""
    al = AuditLogger(data_dir=tmp_path)  # no event_log
    assert al.backend == "jsonl"
    al.log("s1", "tier2_complete", {"plan_steps": 2})

    jsonl = tmp_path / "audit.log"
    assert jsonl.exists()
    line = jsonl.read_text(encoding="utf-8").strip().splitlines()[0]
    assert '"event": "tier2_complete"' in line
    assert '"plan_steps": 2' in line

    tail = al.tail(10)
    assert len(tail) == 1
    assert tail[0]["session_id"] == "s1"


def test_audit_logger_eventlog_failure_falls_back_to_jsonl(
    tmp_path: Path,
) -> None:
    """If the event_log write raises, log() drops to JSONL instead of dying."""
    class BrokenEventLog:
        def append(self, *args, **kwargs):
            raise RuntimeError("simulated sqlite lock")

    al = AuditLogger(
        data_dir=tmp_path,
        event_log=BrokenEventLog(),
    )
    # Should not raise
    al.log("s1", "tier2_complete", {"x": 1})

    # JSONL fallback captured it
    jsonl = tmp_path / "audit.log"
    assert jsonl.exists()
    assert '"event": "tier2_complete"' in jsonl.read_text(encoding="utf-8")
