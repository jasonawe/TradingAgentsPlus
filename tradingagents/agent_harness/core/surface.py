"""Surface routing — classify every orchestrator event into a destination surface.

Why this exists
---------------
Today every event from ``Orchestrator.stream_chat`` flows into a single
SSE pipe. Front-end has to filter at the consumer; the audit log gets
all of them (including noise like ``usage_summary``); the debug log
captures everything at DEBUG level whether useful or not.

The fix is to **tag every event with a surface** so downstream sinks can
filter cheaply:

  - ``ui``        — events the user sees in the chat panel
  - ``audit``     — events recorded in the persistent audit log
  - ``debug``     — events only useful for debugging (token usage,
                    plan replan reasons, intermediate state)
  - ``internal``  — events for harness-internal observability (never
                    surfaced to user or audit)

The default classification is per-event-name (see ``DEFAULT_BY_EVENT``).
New event names default to ``ui`` (conservative — visible).

Wire-in
-------
``SurfaceRouter.tag(event, payload)`` returns a new dict with
``surface`` added. ``SurfaceRouter.route(event, payload)`` dispatches
to sinks (audit / debug) when applicable. ``Orchestrator`` calls
``tag`` on every event it yields and ``route`` for side-effect sinks.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Callable, Optional

LOGGER = logging.getLogger(__name__)


class Surface(str, Enum):
    """Where an event should be sent."""

    UI = "ui"
    AUDIT = "audit"
    DEBUG = "debug"
    INTERNAL = "internal"


# Default classification by orchestrator event name.
# Unmapped event names default to ``Surface.UI`` (visible) — that's the
# safe default because every existing event was already going to UI.
DEFAULT_BY_EVENT: dict[str, Surface] = {
    # UI — visible to user
    "plan_started": Surface.UI,
    "plan_ready": Surface.UI,
    "plan_ready_ptc": Surface.UI,
    "tool_result": Surface.UI,
    "observed": Surface.UI,
    "verified": Surface.UI,
    "agent_final": Surface.UI,
    "answer_verified": Surface.UI,
    "busy": Surface.UI,
    "error": Surface.UI,
    # Debug — only for operators
    "usage_summary": Surface.DEBUG,
    # Audit — persistent record
    "audit": Surface.AUDIT,
}


def classify(event: str) -> Surface:
    """Look up the default surface for ``event``."""
    return DEFAULT_BY_EVENT.get(event, Surface.UI)


class SurfaceRouter:
    """Tag events and dispatch to surface sinks.

    Parameters
    ----------
    audit_sink:
        Optional callable ``(event: str, payload: dict) -> None``
        invoked for every ``Surface.AUDIT`` event.  The orchestrator
    audit (``Orchestrator.audit.log``) is the typical target.
    debug_logger:
        Optional logger used for ``Surface.DEBUG`` events.  Falls back
        to this module's logger when None.
    """

    def __init__(
        self,
        *,
        audit_sink: Optional[Callable[[str, dict], None]] = None,
        debug_logger: Optional[logging.Logger] = None,
    ) -> None:
        self._audit_sink = audit_sink
        self._debug_logger = debug_logger or LOGGER

    def tag(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``payload`` with the ``surface`` field added.

        Does not mutate the input.  If ``payload`` already has a
        ``surface`` field, leave it untouched (caller override).
        """
        if "surface" in payload:
            return payload
        return {**payload, "surface": classify(event).value}

    def route(self, event: str, payload: dict[str, Any]) -> None:
        """Side-effect dispatch to the appropriate sink.

        Safe to call when no sink is configured (no-op).
        """
        surface = classify(event)
        if surface is Surface.AUDIT and self._audit_sink is not None:
            try:
                self._audit_sink(event, payload)
            except Exception:
                LOGGER.debug("audit sink failed for %s", event, exc_info=True)
        elif surface is Surface.DEBUG:
            # DEBUG events always go to DEBUG, even without explicit sink.
            try:
                self._debug_logger.debug(
                    "surface.debug event=%s payload=%s",
                    event, _truncate(payload, max_len=400),
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Introspection (tests + observability)
    # ------------------------------------------------------------------
    @property
    def audit_sink(self) -> Optional[Callable[[str, dict], None]]:
        return self._audit_sink

    @property
    def debug_logger(self) -> logging.Logger:
        return self._debug_logger


def _truncate(obj: Any, max_len: int) -> Any:
    """Best-effort truncate large nested payloads for debug logs."""
    try:
        s = repr(obj)
        if len(s) > max_len:
            return s[:max_len] + "..."
        return obj
    except Exception:
        return "<unrepresentable>"
