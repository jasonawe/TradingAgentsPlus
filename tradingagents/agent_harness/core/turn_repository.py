"""Task 14 — TurnRepository.

Move session bookkeeping out of Orchestrator:
- create_session / touch_session
- add_tokens / get_session / list_sessions
- finalize_projection (mark Runtime run session_projection_state DELIVERED)
- delete_session (coordinate cancellation + store cleanup)

Storage: a thin ``turn_sessions`` table alongside the runtime tables.
Migration is registered as ``003_turn_sessions.sql`` so existing DBs pick
up the new table automatically.
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)


class TurnRepository:
    """Session-level bookkeeping for the supervised AgentRuntime."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._ensure_table()

    @property
    def conn(self) -> sqlite3.Connection:
        return self.store.connection

    # ─── schema bootstrap ────────────────────────────────────

    def _ensure_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS turn_sessions (
              session_id   TEXT PRIMARY KEY,
              created_at   TEXT NOT NULL,
              updated_at   TEXT NOT NULL,
              token_total  INTEGER NOT NULL DEFAULT 0,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self.conn.commit()

    # ─── session lifecycle ──────────────────────────────────

    def create_session(
        self,
        *,
        session_id: str,
        now: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ts = now or datetime.now(timezone.utc).isoformat()
        try:
            self.conn.execute(
                """
                INSERT INTO turn_sessions
                  (session_id, created_at, updated_at, token_total, metadata_json)
                VALUES (?, ?, ?, 0, ?)
                """,
                (session_id, ts, ts, _json_dumps(metadata or {})),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            # 重复创建 — 不抛错,只 touch
            return self.touch_session(session_id=session_id, now=ts)
        return self.get_session(session_id) or {}

    def touch_session(
        self,
        *,
        session_id: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        ts = now or datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "UPDATE turn_sessions SET updated_at=? WHERE session_id=?",
            (ts, session_id),
        )
        if cur.rowcount != 1:
            # 不存在 — create
            return self.create_session(session_id=session_id, now=ts)
        self.conn.commit()
        return self.get_session(session_id) or {}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM turn_sessions WHERE session_id=?",
            (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            import json
            d["metadata"] = json.loads(d.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
        d.pop("metadata_json", None)
        return d

    def list_sessions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM turn_sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [
            self._row_to_session(r) for r in cur.fetchall()
        ]

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            import json
            d["metadata"] = json.loads(d.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
        d.pop("metadata_json", None)
        return d

    # ─── token accounting ───────────────────────────────────

    def add_tokens(
        self,
        *,
        session_id: str,
        prompt: int = 0,
        completion: int = 0,
    ) -> dict[str, Any]:
        """Increment token_total by prompt + completion (creates row if missing)."""
        delta = int(prompt) + int(completion)
        cur = self.conn.execute(
            "UPDATE turn_sessions SET token_total = token_total + ?, "
            "updated_at = ? WHERE session_id = ?",
            (delta, datetime.now(timezone.utc).isoformat(), session_id),
        )
        if cur.rowcount == 0:
            self.create_session(
                session_id=session_id,
                now=datetime.now(timezone.utc).isoformat(),
            )
            self.conn.execute(
                "UPDATE turn_sessions SET token_total = token_total + ? "
                "WHERE session_id = ?",
                (delta, session_id),
            )
        self.conn.commit()
        return self.get_session(session_id) or {}

    # ─── projection finalization ────────────────────────────

    def finalize_projection(
        self,
        *,
        run_id: str,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        """Mark a Runtime run's ``session_projection_state`` DELIVERED.

        Called after L1/L2/L3 final projection has been applied (or
        already-applied via receipt).  Idempotent: re-marking DELIVERED
        is a no-op.
        """
        ts = now or datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "UPDATE agent_runs SET session_projection_state='DELIVERED', "
            "session_projected_at=?, version=version+1, updated_at=? "
            "WHERE run_id=? AND session_projection_state IN ('PENDING','FAILED')",
            (ts, ts, run_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        cur = self.conn.execute(
            "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ─── delete_session — coordinate cancellation + cleanup ─

    def delete_session(
        self,
        *,
        session_id: str,
        now: str | None = None,
    ) -> dict[str, int]:
        """Coordinate AgentRuntime cancellation + store cleanup.

        Sequence:
        1. Mark active runs CANCELLED via agent_runs.state CAS
        2. Cancel RUNNING / READY tasks
        3. Delete child tables(events / outbox / messages / waits / deps)
        4. Delete agent_tasks / agent_runs
        5. Delete turn_sessions row
        """
        ts = now or datetime.now(timezone.utc).isoformat()
        counts = {
            "runs": 0, "tasks": 0, "events": 0,
            "messages": 0, "outbox": 0, "deps": 0, "waits": 0,
        }
        with self.store.serial_write():
            conn = self.conn
            run_rows = conn.execute(
                "SELECT run_id FROM agent_runs WHERE session_id=?",
                (session_id,),
            ).fetchall()
            run_ids = [r[0] for r in run_rows]
            if run_ids:
                placeholders = ",".join("?" * len(run_ids))
                # 1. cancel RUNNING / READY tasks
                cur = conn.execute(
                    f"UPDATE agent_tasks SET state='CANCELLED', "
                    f"version=version+1, updated_at=? WHERE run_id IN ({placeholders}) "
                    f"AND state IN ('READY','PLANNED','RUNNING','WAITING_CHILD','WAITING_MESSAGE','WAITING_APPROVAL')",
                    [ts, *run_ids],
                )
                # 2. delete child tables
                cur = conn.execute(
                    f"DELETE FROM runtime_events WHERE run_id IN ({placeholders})",
                    run_ids,
                )
                counts["events"] = cur.rowcount
                cur = conn.execute(
                    f"DELETE FROM agent_outbox WHERE run_id IN ({placeholders})",
                    run_ids,
                )
                counts["outbox"] = cur.rowcount
                cur = conn.execute(
                    f"DELETE FROM agent_messages WHERE run_id IN ({placeholders})",
                    run_ids,
                )
                counts["messages"] = cur.rowcount
                cur = conn.execute(
                    f"DELETE FROM agent_task_waits WHERE run_id IN ({placeholders})",
                    run_ids,
                )
                counts["waits"] = cur.rowcount
                cur = conn.execute(
                    f"DELETE FROM agent_task_dependencies WHERE run_id IN ({placeholders})",
                    run_ids,
                )
                counts["deps"] = cur.rowcount
                cur = conn.execute(
                    f"DELETE FROM agent_tasks WHERE run_id IN ({placeholders})",
                    run_ids,
                )
                counts["tasks"] = cur.rowcount
            # 3. delete runs
            cur = conn.execute(
                "DELETE FROM agent_runs WHERE session_id=?", (session_id,)
            )
            counts["runs"] = cur.rowcount
            # 4. delete turn_sessions
            conn.execute(
                "DELETE FROM turn_sessions WHERE session_id=?", (session_id,)
            )
            conn.commit()
        return counts


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str)


__all__ = ["TurnRepository"]
