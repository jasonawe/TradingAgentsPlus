"""Harness crash recovery — per-session checkpoints for resume after restart.

Why this exists
---------------
The harness streams events to clients via SSE. If the server crashes
(or restarts for an update) mid-request, the client's connection dies
and they lose the in-flight analysis.  Without checkpoints, the only
recovery option is to restart from scratch — burning tokens and time.

This module provides ``HarnessCheckpointStore`` which persists
per-session state to SQLite.  When ``Orchestrator.stream_chat`` runs,
it calls ``store.save(session_id, ...)`` after each node completes;
when a client wants to resume, ``Orchestrator.resume(session_id)``
loads the checkpoint and replays the buffered events before
continuing.

Wire-in
-------
``HarnessCheckpointStore(store)`` wraps any ``SQLiteStore`` (the same
one settings/web_runs use) — table ``harness_checkpoints`` is added
by migration ``012_harness_checkpoints.sql``.

Checkpoint scope: ``(session_id, state_dict, node_position, emitted_events)``.
We store ``emitted_events`` so resume can replay the events the client
missed (cheap — JSON-list serialisation).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

LOGGER = logging.getLogger(__name__)


# Node positions — match the names used in OrchestratorState nodes.
NODE_PLANNING = "planning"
NODE_EXECUTING = "executing"
NODE_OBSERVING = "observing"
NODE_VERIFYING = "verifying"
NODE_SYNTHESIZING = "synthesizing"
NODE_DONE = "done"

ALL_NODES = (
    NODE_PLANNING, NODE_EXECUTING, NODE_OBSERVING,
    NODE_VERIFYING, NODE_SYNTHESIZING, NODE_DONE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HarnessCheckpoint:
    """Snapshot of a session's in-flight orchestration."""

    session_id: str
    node_position: str
    state: dict[str, Any]
    emitted_events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_row(self) -> dict[str, Any]:
        """Serialise to a row dict ready for SQLiteStore.upsert."""
        return {
            "session_id": self.session_id,
            "state_json": json.dumps(self.state, ensure_ascii=False, default=str),
            "node_position": self.node_position,
            "emitted_events": json.dumps(self.emitted_events, ensure_ascii=False, default=str),
            "token_usage": json.dumps(self.token_usage, ensure_ascii=False, default=str),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> "HarnessCheckpoint":
        """Inverse of to_row. Accepts sqlite3.Row or dict-like."""
        d = dict(row)
        return cls(
            session_id=d["session_id"],
            node_position=d["node_position"],
            state=json.loads(d["state_json"]) if d.get("state_json") else {},
            emitted_events=[
                (ev, payload) for ev, payload in
                json.loads(d.get("emitted_events") or "[]")
            ],
            token_usage=json.loads(d.get("token_usage") or "{}"),
            created_at=d.get("created_at") or _now_iso(),
            updated_at=d.get("updated_at") or _now_iso(),
        )


class HarnessCheckpointStore:
    """Thin wrapper around SQLiteStore for harness checkpoints.

    Designed to be created once per process and shared. All methods
    are synchronous (SQLite is fast enough; checkpoint writes are
    <1ms in practice).
    """

    def __init__(self, store: Any, *, ttl_hours: int = 24) -> None:
        """Wrap any object that can yield a raw sqlite3 connection.

        Accepts:
        - ``SQLiteStore`` directly (has ``_connect()``)
        - Any repository with a ``.store`` attribute pointing at a SQLiteStore
        """
        if hasattr(store, "store") and hasattr(store.store, "_connect"):
            self._store = store.store
        else:
            self._store = store
        self._ttl = timedelta(hours=ttl_hours)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def save(self, checkpoint: HarnessCheckpoint) -> None:
        """Upsert a checkpoint for ``checkpoint.session_id``.

        Called after each node completes in ``Orchestrator._emit``.
        Each save overwrites the previous checkpoint for the same
        session_id (one active checkpoint per session).
        """
        checkpoint.updated_at = _now_iso()
        row = checkpoint.to_row()
        # SQLite upsert — INSERT ... ON CONFLICT(session_id) DO UPDATE SET
        with self._store._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM harness_checkpoints WHERE session_id = ?",
                (checkpoint.session_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE harness_checkpoints SET state_json=?, node_position=?, "
                    "emitted_events=?, token_usage=?, updated_at=? WHERE session_id=?",
                    (row["state_json"], row["node_position"], row["emitted_events"],
                     row["token_usage"], row["updated_at"], checkpoint.session_id),
                )
            else:
                conn.execute(
                    "INSERT INTO harness_checkpoints (session_id, state_json, "
                    "node_position, emitted_events, token_usage, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row["session_id"], row["state_json"], row["node_position"],
                     row["emitted_events"], row["token_usage"],
                     row["created_at"], row["updated_at"]),
                )
            conn.commit()

    def delete(self, session_id: str) -> bool:
        """Remove the checkpoint for ``session_id``. Returns True if removed."""
        with self._store._connect() as conn:
            cur = conn.execute(
                "DELETE FROM harness_checkpoints WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def load(self, session_id: str) -> HarnessCheckpoint | None:
        """Load the checkpoint for ``session_id`` or None."""
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM harness_checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return HarnessCheckpoint.from_row(row) if row else None

    def has_checkpoint(self, session_id: str) -> bool:
        return self.load(session_id) is not None

    def list_active(self, *, limit: int = 100) -> list[str]:
        """List session_ids with active checkpoints (newest first)."""
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT session_id FROM harness_checkpoints "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    def purge_stale(self, *, now: datetime | None = None) -> int:
        """Delete checkpoints older than ``ttl_hours``.

        Intended to be called periodically (e.g. on app startup) to
        bound the table size. Returns the number of rows deleted.

        ``now`` is exposed for tests; defaults to ``datetime.now(utc)``.
        """
        reference_now = now if now is not None else datetime.now(timezone.utc)
        cutoff = (reference_now - self._ttl).isoformat()
        with self._store._connect() as conn:
            cur = conn.execute(
                "DELETE FROM harness_checkpoints WHERE updated_at < ?",
                (cutoff,),
            )
            conn.commit()
            if cur.rowcount:
                LOGGER.info("purged %d stale harness checkpoints (cutoff=%s)",
                            cur.rowcount, cutoff)
            return cur.rowcount


def make_session_id(prefix: str = "harness") -> str:
    """Generate a fresh session id.

    Stable shape: ``{prefix}-{8-hex}`` — same as the existing
    ``f"harness-{__import__('uuid').uuid4().hex[:8]}"`` in
    ``/api/harness/chat``.
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
