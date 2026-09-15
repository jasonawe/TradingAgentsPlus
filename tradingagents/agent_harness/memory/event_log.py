"""L1 append-only event log (W3-D5 R1).

dsh pattern: every per-session interaction is one ``Event{seq, type,
data, time}`` row. The LLM context is DERIVED from the log (filter by
``type='message'`` events), not stored separately. Audit + replay +
replace are trivial because the log IS the source of truth.

Spec::

    R1  L1 append-only event log
        Event{seq, type, data, time}
        LLM 上下文从 log 派生          1d  极高  dsh session log + surface

Why this exists
---------------
Today ``SqliteSessionMemory`` stores chat history as a single
``history`` key — a list blob keyed by ``session:{id}:history``.
Mutation is destructive (replace-the-whole-list on every append). No
audit trail. No replay. No replace-by-seq.

This module is an ADDITIVE, side-by-side log:

  - Each append gets the next ``seq`` per session (monotonic, no gaps)
  - The log is append-only by contract: ``replace(seq, ...)`` records
    a "tombstone" event (type ``replace``) instead of mutating in
    place, so the original is always recoverable
  - ``messages(session_id)`` derives the LLM-facing chat history by
    filtering ``type='message'`` events, sorted by seq

Wiring
------
Harness owns one ``EventLog`` instance (``harness.event_log``); the
orchestrator appends events as the run progresses. Plugin code can
subscribe to ``EventBus`` and forward events into the log via
``harness.events.subscribe('llm.complete', _forward, mode='emit')``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

DEFAULT_DB_PATH = Path(".ta_cache") / "event_log.sqlite"


class SurfaceType(str, Enum):
    """W3-D6 R2 — surface classification for events.

    The 4 surface types feed the derived LLM history (and are visible
    to the front-end). Everything else is log-only — kept in the audit
    trail but not exposed to the LLM.
    """

    SYSTEM = "system"          # system prompt + LLM-bound context
    USER = "user"              # user input
    ASSISTANT = "assistant"    # LLM response (full message or final stream chunk)
    TOOL = "tool"              # tool execution result
    LOG = "log"                # audit-only — never shown to the LLM


# Mapping from event type string → surface classification. Anything not
# in this table defaults to ``SurfaceType.LOG`` so the derived history
# stays clean by default.
TYPE_TO_SURFACE: dict[str, SurfaceType] = {
    "system/message":    SurfaceType.SYSTEM,
    "user/message":      SurfaceType.USER,
    "assistant/message": SurfaceType.ASSISTANT,
    "assistant/delta":   SurfaceType.ASSISTANT,  # streaming chunk
    "tool/result":       SurfaceType.TOOL,
    "replace":           SurfaceType.LOG,         # audit-only
}


def classify_surface(type_: str) -> SurfaceType:
    return TYPE_TO_SURFACE.get(type_, SurfaceType.LOG)


@dataclass(frozen=True)
class Event:
    """One row in the append-only log.

    ``seq`` is monotonic per ``session_id``; the first event for a
    session has seq=1. ``time`` is UNIX seconds (UTC). ``data`` is
    whatever the producer serialised — the log is type-agnostic.

    ``surface`` (W3-D6 R2) is derived from ``type`` at construction
    time; surface events flow into the LLM history, log-only ones
    stay in the audit trail.
    """

    seq: int
    session_id: str
    type: str
    data: Any
    time: float
    replaced_by: Optional[int] = None  # seq of the replace event, if any
    surface: SurfaceType = field(default=SurfaceType.LOG)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "session_id": self.session_id,
            "type": self.type,
            "data": self.data,
            "time": self.time,
            "replaced_by": self.replaced_by,
            "surface": self.surface.value,
        }

    @property
    def time_iso(self) -> str:
        return datetime.fromtimestamp(self.time, tz=timezone.utc).isoformat()

    @property
    def is_surface(self) -> bool:
        """True when this event feeds the derived LLM history."""
        return self.surface != SurfaceType.LOG


class EventLog:
    """Append-only per-session event log.

    The schema is a single table::

        event_log (
          seq          INTEGER NOT NULL,
          session_id   TEXT    NOT NULL,
          type         TEXT    NOT NULL,
          data         TEXT    NOT NULL,           -- JSON-serialised
          time         REAL    NOT NULL,
          PRIMARY KEY (session_id, seq)
        )

    ``seq`` is allocated under a per-session lock so concurrent writers
    don't collide. A separate ``replace`` event (type='replace') is the
    audit-friendly way to "modify" an existing event — see
    :meth:`replace`.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Storage helpers
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        # Enforce FK + give us ON CONFLICT for the upsert path
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_log (
                    seq INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    time REAL NOT NULL,
                    replaced_by INTEGER,
                    PRIMARY KEY (session_id, seq)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_log_type "
                "ON event_log(session_id, type)"
            )
            conn.commit()

    @staticmethod
    def _serialize(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, default=str)

    @staticmethod
    def _deserialize(raw: str) -> Any:
        try:
            return json.loads(raw)
        except Exception:
            return raw

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------
    def append(
        self, session_id: str, type: str, data: Any, *, ts: float | None = None,
    ) -> Event:
        """Append one event to ``session_id``'s log.

        Returns the persisted :class:`Event` with the allocated ``seq``.
        ``ts`` defaults to now (UNIX seconds, UTC); tests can pin a
        value to make assertions deterministic.
        """
        if not session_id or not isinstance(session_id, str):
            raise ValueError("session_id must be a non-empty string")
        if not type or not isinstance(type, str):
            raise ValueError("type must be a non-empty string")
        if ts is None:
            import time as _time
            ts = _time.time()
        payload = self._serialize(data)
        surface = classify_surface(type)
        with self._lock, self._connect() as conn:
            # Allocate next seq under the global lock so concurrent
            # appends can't collide on the (session_id, seq) PK.
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM event_log WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            next_seq = int(row["m"]) + 1
            conn.execute(
                "INSERT INTO event_log(seq, session_id, type, data, time) "
                "VALUES (?, ?, ?, ?, ?)",
                (next_seq, session_id, type, payload, ts),
            )
            conn.commit()
        return Event(
            seq=next_seq, session_id=session_id, type=type, data=data,
            time=ts, surface=surface,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        type_filter: str | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """Return events in seq order.

        ``after_seq``: skip events with ``seq <= after_seq`` (handy for
        incremental SSE replay). ``type_filter``: restrict to one type
        (e.g. ``"message"`` to derive the LLM chat history).
        """
        clauses = ["session_id = ?", "seq > ?"]
        params: list[Any] = [session_id, after_seq]
        if type_filter is not None:
            clauses.append("type = ?")
            params.append(type_filter)
        sql = (
            "SELECT seq, type, data, time FROM event_log WHERE "
            + " AND ".join(clauses)
            + " ORDER BY seq ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Event(
                seq=int(r["seq"]),
                session_id=session_id,
                type=r["type"],
                data=self._deserialize(r["data"]),
                time=float(r["time"]),
                surface=classify_surface(r["type"]),
            )
            for r in rows
        ]

    def __iter__(self) -> Iterator[Event]:  # pragma: no cover - convenience
        return iter(self.events("*"))  # type: ignore[arg-type]

    def get(self, session_id: str, seq: int) -> Event | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT seq, type, data, time, replaced_by FROM event_log "
                "WHERE session_id = ? AND seq = ?",
                (session_id, seq),
            ).fetchone()
        if not row:
            return None
        return Event(
            seq=int(row["seq"]),
            session_id=session_id,
            type=row["type"],
            data=self._deserialize(row["data"]),
            time=float(row["time"]),
            replaced_by=int(row["replaced_by"]) if row["replaced_by"] is not None else None,
            surface=classify_surface(row["type"]),
        )

    # ------------------------------------------------------------------
    # Derived views
    # ------------------------------------------------------------------
    def messages(self, session_id: str, *, after_seq: int = 0) -> list[Event]:
        """Return ``type='message'`` events (the LLM chat history)."""
        return self.events(session_id, after_seq=after_seq, type_filter="message")

    # W3-D6 R2: surface query — returns only events that feed the
    # derived LLM history (system / user / assistant / tool). log-only
    # events are skipped. The full log is still available via
    # ``events()`` for audit.
    SURFACE_TYPES: tuple[str, ...] = (
        "system/message", "user/message",
        "assistant/message", "assistant/delta",
        "tool/result",
    )

    def surface_events(
        self, session_id: str, *, after_seq: int = 0,
    ) -> list[Event]:
        """Return surface-classified events in seq order.

        Used to derive the LLM chat history — system prompt is the
        first event (committed once at session start), followed by
        user / assistant / tool interleaved by seq.
        """
        clauses = [
            "session_id = ?",
            "seq > ?",
            "type IN ({})".format(",".join("?" for _ in self.SURFACE_TYPES)),
        ]
        params: list[Any] = [session_id, after_seq, *self.SURFACE_TYPES]
        sql = (
            "SELECT seq, type, data, time, replaced_by FROM event_log WHERE "
            + " AND ".join(clauses)
            + " ORDER BY seq ASC"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Event(
                seq=int(r["seq"]),
                session_id=session_id,
                type=r["type"],
                data=self._deserialize(r["data"]),
                time=float(r["time"]),
                replaced_by=int(r["replaced_by"]) if r["replaced_by"] is not None else None,
                surface=classify_surface(r["type"]),
            )
            for r in rows
        ]

    def chat_history(
        self, session_id: str, *, apply_replaces: bool = True,
    ) -> list[dict[str, Any]]:
        """Project the full surface chain into LLM messages.

        W3-D6 R3: system prompt (``type='system/message'``) is the
        first event; user/assistant/tool follow in seq order. The
        result is the canonical LLM history — derived from the log,
        not stored separately.

        apply_replaces=True (default) walks the replace chain so
        the LLM sees the LATEST data — e.g. if the system prompt
        was re-committed, the history reflects the new prompt. Set
        to False for strict replay (original data only).
        """
        out: list[dict[str, Any]] = []
        for ev in self.surface_events(session_id):
            data = ev.data if isinstance(ev.data, dict) else {"content": ev.data}
            content = data.get("content", "")
            if ev.type == "system/message":
                role = "system"
            elif ev.type == "tool/result":
                role = "tool"
            elif ev.type.startswith("assistant/"):
                role = "assistant"
            else:
                role = data.get("role", "user")
            if apply_replaces and ev.replaced_by is not None:
                rep = self.get(session_id, ev.replaced_by)
                if rep and isinstance(rep.data, dict):
                    new_data = rep.data.get("new_data", data)
                    if isinstance(new_data, dict):
                        content = new_data.get("content", content)
            out.append({
                "role": role,
                "content": content,
                "ts": ev.time,
                "seq": ev.seq,
                "surface": ev.surface.value,
            })
        return out

    def commit_system_prompt(
        self, session_id: str, prompt: str, *, ts: float | None = None,
    ) -> Event:
        """W3-D6 R3: commit the system prompt as a surface event.

        Idempotent: if a ``system/message`` event already exists for
        this session, the existing one is replaced (audit-tombstoned)
        rather than appending a second one. New sessions get a fresh
        commit. ``replace`` is used so the audit trail still shows
        the old prompt.
        """
        existing = next(
            (e for e in self.events(session_id, type_filter="system/message")),
            None,
        )
        if existing is not None:
            return self.replace(
                session_id, existing.seq,
                {"content": prompt, "role": "system"},
                reason="system prompt re-commit",
            )
        return self.append(
            session_id, "system/message",
            {"content": prompt, "role": "system"},
            ts=ts,
        )

    # ------------------------------------------------------------------
    # Replace (audit-friendly mutation)
    # ------------------------------------------------------------------
    def replace(
        self, session_id: str, seq: int, new_data: Any, *, reason: str | None = None,
    ) -> Event:
        """Mark event ``(session_id, seq)`` as replaced.

        Does NOT mutate the original row (append-only contract). Instead
        appends a new ``type='replace'`` event pointing at the old one.
        The old event's ``replaced_by`` is also updated in place for
        cheap lookups — but the original ``data`` column stays.

        ``reason`` is optional free-form text (e.g. "redact PII",
        "user rephrased", "tool output corrected").
        """
        original = self.get(session_id, seq)
        if original is None:
            raise KeyError(f"event ({session_id}, {seq}) not found")
        replace_event = self.append(
            session_id,
            "replace",
            {
                "target_seq": seq,
                "new_data": new_data,
                "reason": reason,
            },
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE event_log SET replaced_by = ? "
                "WHERE session_id = ? AND seq = ?",
                (replace_event.seq, session_id, seq),
            )
            conn.commit()
        return replace_event

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def head_seq(self, session_id: str) -> int:
        """Return the current max seq for ``session_id`` (0 if empty)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM event_log WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["m"])

    def count(self, session_id: str | None = None) -> int:
        if session_id is None:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()
            return int(row["n"])
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM event_log WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["n"])

    def clear(self, session_id: str) -> int:
        """Delete all events for ``session_id``. Returns rows removed.

        Provided for test cleanup + admin tooling; production code
        should generally use ``replace`` for audit-grade mutations.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM event_log WHERE session_id = ?", (session_id,),
            )
            conn.commit()
            return cur.rowcount

    def __len__(self) -> int:
        return self.count()
