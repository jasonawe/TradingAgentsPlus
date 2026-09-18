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

# §Step 29 — milestone ordering. Used by resume_from to find the
# latest checkpoint at-or-before the requested node. Lower index =
# earlier in the pipeline. Any node not in this tuple is treated as
# a free-form observation event (no partial-replay anchor).
_NODE_ORDER: dict[str, int] = {n: i for i, n in enumerate(ALL_NODES)}

# Legacy milestone_id used to tag rows imported from the pre-015
# schema (single-row-per-session). load_latest() and resume() both
# honour these rows as if they were the last milestone in the chain.
LEGACY_MILESTONE_ID = "legacy:singleton"

# Emitted-event monotonic counter per (session, node). Stored on the
# store instance so two saves for the same node in the same session
# still get unique milestone_ids.
_EMIT_COUNTERS: dict[tuple[str, str], int] = {}


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
    # §Step 29 — composite key part. "<node>:<counter>" for new
    # saves, "legacy:singleton" for rows imported from pre-015
    # schema. Counters are monotonic per (session_id, node).
    milestone_id: str = LEGACY_MILESTONE_ID

    def to_row(self) -> dict[str, Any]:
        """Serialise to a row dict ready for SQLiteStore.upsert."""
        return {
            "session_id": self.session_id,
            "milestone_id": self.milestone_id,
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
            milestone_id=d.get("milestone_id") or LEGACY_MILESTONE_ID,
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
        """Upsert a milestone checkpoint for ``checkpoint.session_id``.

        Step 29 — composite key (session_id, milestone_id). Each
        milestone becomes its own row, so a single session can have
        a chain of recoverable snapshots (planning -> executing ->
        observing -> verifying -> synthesizing -> done).

        If the caller did not set milestone_id we synthesise one
        of the form <node>:<counter> where counter is monotonic
        per (session_id, node) — guaranteeing uniqueness without
        requiring callers to coordinate.
        """
        checkpoint.updated_at = _now_iso()
        if not checkpoint.milestone_id or checkpoint.milestone_id == LEGACY_MILESTONE_ID:
            counter = _EMIT_COUNTERS.get(
                (checkpoint.session_id, checkpoint.node_position), 0,
            ) + 1
            _EMIT_COUNTERS[(checkpoint.session_id, checkpoint.node_position)] = counter
            checkpoint.milestone_id = f"{checkpoint.node_position}:{counter}"
        row = checkpoint.to_row()
        with self._store._connect() as conn:
            conn.execute(
                "INSERT INTO harness_checkpoints "
                "(session_id, milestone_id, state_json, node_position, "
                "emitted_events, token_usage, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, milestone_id) DO UPDATE SET "
                "state_json=excluded.state_json, node_position=excluded.node_position, "
                "emitted_events=excluded.emitted_events, token_usage=excluded.token_usage, "
                "updated_at=excluded.updated_at",
                (row["session_id"], row["milestone_id"], row["state_json"],
                 row["node_position"], row["emitted_events"], row["token_usage"],
                 row["created_at"], row["updated_at"]),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Step 29 — partial-replay read paths
    # ------------------------------------------------------------------
    def load_latest(self, session_id: str) -> HarnessCheckpoint | None:
        """Return the milestone with the highest updated_at for session_id.

        Falls back to legacy:singleton for sessions written before
        the milestone migration (those rows always sort last because
        their updated_at is the last-write timestamp).
        """
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM harness_checkpoints WHERE session_id = ? "
                "ORDER BY updated_at DESC, milestone_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return HarnessCheckpoint.from_row(row)

    def load_at_or_before(
        self, session_id: str, node_position: str,
    ) -> HarnessCheckpoint | None:
        """Return the latest milestone whose node is at or before node_position.

        Used by Orchestrator.resume_from to start replay from the
        nearest successful checkpoint. node_position must be a
        member of ALL_NODES; unknown nodes fall back to
        load_latest.
        """
        if node_position not in _NODE_ORDER:
            return self.load_latest(session_id)
        target_idx = _NODE_ORDER[node_position]
        candidates = self.list_milestones(session_id)
        # Sort by (node_index ascending, updated_at descending) so we
        # pick the latest checkpoint at the *latest* node <= target.
        # Walk candidates in (node_index DESC, updated_at DESC) order
        # and return the first whose node_index <= target_idx. That
        # way "at or before synthesizing" picks the latest of
        # {planning, executing, observing, synthesizing} (NOT done).
        candidates.sort(
            key=lambda c: (
                -_NODE_ORDER.get(c.node_position, -1),
                -datetime.fromisoformat(c.updated_at).timestamp(),
            ),
        )
        for c in candidates:
            if _NODE_ORDER.get(c.node_position, 999) <= target_idx:
                return c
        return None

    def list_milestones(self, session_id: str) -> list[HarnessCheckpoint]:
        """All milestones for session_id ordered by updated_at ascending."""
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM harness_checkpoints WHERE session_id = ? "
                "ORDER BY updated_at ASC, milestone_id ASC",
                (session_id,),
            ).fetchall()
        return [HarnessCheckpoint.from_row(r) for r in rows]

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
        """Load the latest checkpoint for ``session_id`` or None.

        Step 29 — alias for load_latest (legacy callers). Returns
        the milestone with the highest updated_at.
        """
        return self.load_latest(session_id)

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
