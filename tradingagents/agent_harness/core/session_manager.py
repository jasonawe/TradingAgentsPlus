"""SessionManager — single facade over session-scoped storage (roadmap §1.3 A3).

Why
----
Session state lives in four places:
  - ``web_runs.sqlite3:sessions`` (SessionStore — metadata)
  - ``web_runs.sqlite3:harness_checkpoints`` (HarnessCheckpointStore — state)
  - ``event_log.sqlite:event_log`` (EventLog — SurfaceOp events keyed by session_id)
  - ``{data_dir}/agent_general/sessions/agent_<safe_id>.db`` (L1 langgraph)

Until now, ``SessionStore.delete()`` only cleaned the first two; the
LG file leaked and the EventLog accumulated orphan rows. This facade
owns all four and exposes one ``delete(session_id)`` that cleans them
in deterministic order.

Wire-in
-------
Harness.__init__ constructs the manager with whichever of the four
collaborators are present; missing collaborators are silently skipped
(e.g. tests that only wire EventLog don't need SessionStore). The
manager stays a thin facade — no behaviour moves here, just orchestration.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class SessionManager:
    """Single point of entry for cross-store session lifecycle ops.

    Every collaborator is optional so the manager can be constructed
    in tests that only wire a subset (e.g. only the EventLog). At least
    one of ``session_store / l1 / event_log`` should be present for the
    manager to be useful.
    """

    def __init__(
        self,
        *,
        session_store: Any | None = None,
        checkpoint_store: Any | None = None,
        l1: Any | None = None,
        event_log: Any | None = None,
    ) -> None:
        self.session_store = session_store
        self.checkpoint_store = checkpoint_store
        self.l1 = l1
        self.event_log = event_log

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get(self, session_id: str) -> dict[str, Any] | None:
        """Return the Session metadata row, or None if not found."""
        if self.session_store is None:
            return None
        session = self.session_store.get(session_id)
        return session.to_dict() if session is not None else None

    def list_sessions(
        self,
        *,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if self.session_store is None:
            return []
        rows = self.session_store.list_sessions(
            user_id=user_id, status=status, limit=limit, offset=offset,
        )
        return [r.to_dict() for r in rows]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def upsert(self, session: Any) -> None:
        if self.session_store is None:
            return
        self.session_store.upsert(session)

    def touch(
        self, session_id: str, *, message_delta: int = 1,
        token_delta: int = 0,
    ) -> bool:
        """Bump ``last_active`` + counters on the metadata row."""
        if self.session_store is None:
            return False
        return self.session_store.touch(
            session_id,
            message_delta=message_delta,
            token_delta=token_delta,
        )

    # ------------------------------------------------------------------
    # Delete — the headline method. Cleans every store in turn.
    # ------------------------------------------------------------------
    def delete(self, session_id: str) -> dict[str, Any]:
        """Hard-delete session from every collaborator.

        Returns a dict of counts per source so callers can log / audit::

            {
                "sessions": 1,
                "harness_checkpoints": 1,
                "event_log_rows": 47,
                "l1_lg_files": 1,
            }

        Order matters: we delete from the cheap in-memory indexes first
        (rows in SQLite), then drop the file. Each step is best-effort:
        if any one throws, we log and keep going so a stale EventLog row
        doesn't prevent the user-visible DELETE from succeeding.
        """
        counts: dict[str, Any] = {
            "sessions": 0,
            "harness_checkpoints": 0,
            "event_log_rows": 0,
            "l1_lg_files": 0,
        }

        # 1) Session metadata + harness checkpoints — same DB.
        if self.session_store is not None:
            try:
                result = self.session_store.delete(session_id)
                counts["sessions"] = result.get("sessions", 0)
                counts["harness_checkpoints"] = result.get(
                    "harness_checkpoints", 0,
                )
            except Exception as e:  # pragma: no cover - defensive
                LOGGER.warning(
                    "SessionManager.delete: session_store cleanup failed "
                    "for %s: %s", session_id, e,
                )

        # 2) Explicit checkpoint_store handle (when not folded into
        # session_store.delete).
        if self.checkpoint_store is not None and self.checkpoint_store is not \
                getattr(self.session_store, "_checkpoint_store", None):
            try:
                self.checkpoint_store.delete(session_id)
            except Exception as e:  # pragma: no cover - defensive
                LOGGER.warning(
                    "SessionManager.delete: checkpoint_store cleanup "
                    "failed for %s: %s", session_id, e,
                )

        # 3) EventLog rows — clear every row whose session_id matches.
        # We DON'T use SurfaceOp.replace because the user's intent is a
        # hard delete, not a redact. SurfaceOp only covers replaceable
        # types (system / user / assistant / tool messages) and the LG
        # ``archive/legacy_history`` rows live outside that surface
        # anyway — clean sweep is correct.
        if self.event_log is not None:
            try:
                counts["event_log_rows"] = self.event_log.clear(session_id)
            except Exception as e:  # pragma: no cover - defensive
                LOGGER.warning(
                    "SessionManager.delete: event_log cleanup failed "
                    "for %s: %s", session_id, e,
                )

        # 4) L1 LangGraph checkpointer file — best-effort unlink.
        # Only the LG backend has a per-session file; the legacy
        # single-table path stores everything in session_memory.sqlite
        # and the row cleanup happens via event_log.clear (legacy
        # rows weren't split by session_id there).
        if self.l1 is not None:
            db_path = self._l1_lg_db_path(session_id)
            if db_path is not None and db_path.exists():
                try:
                    db_path.unlink()
                    counts["l1_lg_files"] = 1
                except Exception as e:  # pragma: no cover - defensive
                    LOGGER.warning(
                        "SessionManager.delete: L1 LG file cleanup failed "
                        "for %s (%s): %s", session_id, db_path, e,
                    )

        return counts

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _l1_lg_db_path(self, session_id: str) -> Path | None:
        """Best-effort resolve of the per-session LG file path."""
        l1 = self.l1
        if l1 is None:
            return None
        try:
            # SqliteSessionMemory exposes ``_lg_db_path`` as the canonical
            # resolver (regression-tested in test_l1_lg_backend_regression).
            return l1._lg_db_path(session_id)
        except Exception:
            return None


__all__ = ["SessionManager"]
