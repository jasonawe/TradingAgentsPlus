"""Task 18 — TraceProjector (dual-view).

Read durable events from runtime_events and project to:
- ``user`` view: safe-for-frontend payload, secrets redacted
- ``inspector`` view: full diagnostic detail (tasks, messages, evidence,
  timing, token usage)

Control events (``done``, ``resume_complete``) are **never persisted** —
the connection layer appends them after durable replay.

Cursor pagination: ``since_seq`` + ``limit`` (max 100).
"""
from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


# Fields that must be stripped from ``user`` view (secrets / internals).
_USER_VIEW_REDACT = frozenset({
    "prompt", "system_prompt", "authorization", "api_key", "token",
    "secret", "password", "raw_llm_response",
})


# Control event names — never persisted to runtime_events.
_PERSISTED_EVENTS = frozenset({
    "agent_progress", "repair_started", "handoff_requested",
    "waiting_user", "run_recovered", "task_state",
    "message_persisted", "tool_invocation",
})


class TraceProjector:
    """Read + project runtime_events for SSE / REST consumers."""

    def __init__(self, *, store: Any) -> None:
        self.store = store

    @property
    def PERSISTED_EVENTS(self) -> frozenset:
        return _PERSISTED_EVENTS

    # ─── public projection ──────────────────────────────────

    def project(
        self,
        *,
        run_id: str,
        view: str = "user",
        since_seq: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Project events for ``run_id``.

        ``view`` ∈ {"user", "inspector"}.
        Returns events with ``seq > since_seq`` (asc), capped at
        ``min(limit, 100)``.
        """
        limit = max(1, min(int(limit), 100))
        rows = self.store.connection.execute(
            "SELECT * FROM runtime_events "
            "WHERE run_id=? AND seq > ? "
            "ORDER BY seq ASC LIMIT ?",
            (run_id, int(since_seq), limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            payload = d.get("payload_json")
            if isinstance(payload, str):
                import json
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            elif payload is None:
                payload = {}
            if view == "user":
                payload = self._redact_user(payload)
            out.append({
                "event_id": d.get("event_id"),
                "run_id": d.get("run_id"),
                "seq": d.get("seq"),
                "event_type": d.get("event_type"),
                "task_id": d.get("task_id"),
                "message_id": d.get("message_id"),
                "surface": d.get("surface"),
                "payload": payload,
                "created_at": d.get("created_at"),
            })
        return out

    # ─── connection-only control events ─────────────────────

    def append_control(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a connection-only event (NOT persisted).

        Used by SSE handlers to attach ``done`` / ``resume_complete`` after
        replaying durable events.  The returned dict is what the consumer
        sees — the runtime_events table never records this row.
        """
        return {
            "event_type": event_type,
            "run_id": run_id,
            "connection_only": True,
            "payload": dict(payload or {}),
        }

    # ─── helpers ────────────────────────────────────────────

    @staticmethod
    def _redact_user(payload: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in (payload or {}).items():
            if key.lower() in _USER_VIEW_REDACT:
                continue
            out[key] = value
        return out


__all__ = ["TraceProjector"]
