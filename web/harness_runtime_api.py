"""Task 22 — Harness Runtime API adapters.

Framework-light handlers that can be mounted on FastAPI in Task 24.
Each handler accepts the harness + request body and yields a sequence
of event dicts (for SSE) or returns a single response (for JSON POST).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

LOGGER = logging.getLogger(__name__)


def chat_handler(
    *,
    harness: Any,
    session_id: str,
    body: dict[str, Any],
    route: Any,
) -> Iterable[dict[str, Any]]:
    """Adapter for the existing chat endpoint.

    Yields the same envelope the legacy ``stream_chat`` produces so the
    UI doesn't need to change yet.
    """
    user_message = body.get("user_message") or body.get("message") or ""
    orchestrator = getattr(harness, "orchestrator", None)
    if orchestrator is None or not hasattr(orchestrator, "stream_chat"):
        yield {"type": "error", "reason": "no orchestrator"}
        return
    try:
        for event in orchestrator.stream_chat(
            session_id=session_id, user_message=user_message,
            route=route, client_request_id=body.get("client_request_id"),
        ):
            yield event
    except Exception as exc:
        LOGGER.exception("chat_handler failed: %s", exc)
        yield {"type": "error", "reason": str(exc)}


def resume_handler(
    *,
    harness: Any,
    run_id: str,
    after_seq: int = 0,
) -> Iterable[dict[str, Any]]:
    """Resume events for ``run_id`` with ``after_seq`` cursor."""
    runtime = getattr(harness, "runtime", None)
    if runtime is None or not hasattr(runtime, "stream"):
        return iter([])
    try:
        for ev in runtime.stream(run_id=run_id, since_seq=int(after_seq)):
            yield ev
    except Exception as exc:
        LOGGER.exception("resume_handler failed: %s", exc)
        yield {"event_type": "error", "reason": str(exc)}


def trace_handler(
    *,
    harness: Any,
    run_id: str,
    view: str = "user",
    since_seq: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Trace projector endpoint."""
    projector = getattr(harness, "trace_projector", None)
    if projector is None:
        return []
    return projector.project(
        run_id=run_id, view=view, since_seq=since_seq, limit=limit,
    )


__all__ = ["chat_handler", "resume_handler", "trace_handler"]
