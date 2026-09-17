"""AuditLogger — single-source audit pipeline (v3 spec §7.2 #10).

Originally wrote JSONL into ``audit.log`` alongside ``write_audit_log``
(web_runs.sqlite3) and the SurfaceOp event log (event_log.sqlite). That
triple-storage setup left the JSONL file as dead weight — production
never read it, only the orchestrator's ``tier2_complete`` events
were written there.

Now: when the harness wires an :class:`EventLog` into the
``AuditLogger``, lifecycle events flow into the same SQLite store as
the conversation SurfaceOp events (``type=audit/<event>``,
``surface=log`` — never enters LLM context). Standalone / test usage
without an EventLog keeps the JSONL fallback so existing tests stay
green.

Compliance-grade destructive-tool audit continues to live in
``web_runs.sqlite3.write_audit_log`` (see ``tradingagents.agent_harness.audit``)
— that's the immutable HITL trail that can't be SurfaceOp-replaced.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

# Type prefix used when routing audit events into the EventLog.
# Conversation events use ``assistant/message`` / ``user/message`` etc;
# audit/lifecycle events use ``audit/<event>`` so the prefix query in
# ``tail()`` can fish them out without touching the LLM-bound surface
# chain.
_EVENT_LOG_PREFIX = "audit/"


class AuditLogger:
    def __init__(
        self,
        data_dir: str | Path | None = None,
        event_log: Any | None = None,
    ) -> None:
        """Construct an audit logger.

        Args:
            data_dir: legacy JSONL destination (only used when
                ``event_log`` is not provided). Defaults to
                ``~/.tradingagents`` (or ``$TRADINGAGENTS_DATA_DIR``).
            event_log: optional ``EventLog`` instance. When supplied
                (the harness wires this up), lifecycle events are
                appended to it instead of the JSONL file.
        """
        self._event_log = event_log

        # JSONL fallback path; only used when ``event_log`` is None.
        self._data_dir = Path(data_dir) if data_dir else Path(
            os.environ.get("TRADINGAGENTS_DATA_DIR", ".ta_cache"),
        )
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._data_dir / "audit.log"

    @property
    def backend(self) -> str:
        """Return ``"event_log"`` or ``"jsonl"`` so callers can adapt."""
        return "event_log" if self._event_log is not None else "jsonl"

    def log(
        self,
        session_id: str,
        event: str,
        payload: dict | None = None,
    ) -> None:
        if self._event_log is not None:
            try:
                self._event_log.append(
                    session_id,
                    f"{_EVENT_LOG_PREFIX}{event}",
                    payload or {},
                )
                return
            except Exception as e:  # pragma: no cover - defensive
                LOGGER.warning(
                    "audit log write via event_log failed (%s); "
                    "falling back to JSONL", e,
                )

        # JSONL fallback (standalone tests, or event_log write failed).
        record = {
            "ts": time.time(),
            "session_id": session_id,
            "event": event,
            "payload": payload or {},
        }
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            LOGGER.warning("audit log JSONL write failed: %s", e)

    def tail(self, n: int = 50) -> list[dict]:
        """Return the most recent ``n`` audit events (across all sessions).

        Output shape mirrors the legacy JSONL record so callers can
        consume either backend transparently::

            {"ts": float, "session_id": str, "event": str, "payload": dict}
        """
        if self._event_log is not None:
            try:
                events = self._event_log.events_by_type_prefix(
                    session_id=None,
                    prefix=_EVENT_LOG_PREFIX,
                    limit=n,
                )
                # ``events_by_type_prefix`` returns DESC by time; tail
                # caller expects most-recent-first so this is correct.
                return [
                    {
                        "ts": float(e.time),
                        "session_id": e.session_id,
                        "event": e.type.removeprefix(_EVENT_LOG_PREFIX),
                        "payload": e.data if isinstance(e.data, dict) else {},
                    }
                    for e in events
                ]
            except Exception as e:  # pragma: no cover - defensive
                LOGGER.warning(
                    "audit log read via event_log failed (%s); "
                    "falling back to JSONL", e,
                )

        if not self._path.exists():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()[-n:]
            return [json.loads(line) for line in lines]
        except Exception:
            return []
