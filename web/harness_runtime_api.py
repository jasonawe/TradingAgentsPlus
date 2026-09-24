"""Task 22 — Harness Runtime API adapters.

Framework-light handlers that can be mounted on FastAPI in Task 24.
Each handler accepts the harness + request body and yields a sequence
of event dicts (for SSE) or returns a single response (for JSON POST).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

LOGGER = logging.getLogger(__name__)


async def chat_handler(
    *,
    harness: Any,
    session_id: str,
    body: dict[str, Any],
    route: Any,
) -> Iterable[tuple[str, dict[str, Any]]]:
    """Task 24 — chat endpoint adapter that drives V2 runtime.

    Behavior:
    1. Open a V2 run via ``runtime.start_analysis()`` so the turn is
       persisted in the runtime SQLite tables (agent_runs / agent_tasks
       / runtime_events). This is what makes the runtime DB non-empty in
       production — it captures every chat turn.
    2. Delegate the actual chat to the V1 Orchestrator (its 5-node
       state machine is battle-tested across 84 tests and 27 test files
       importing Orchestrator). The runtime persists the start/end of
       the run; the orchestrator drives the LLM/tool calls.
    3. Yield the same envelope V1 produces so the UI doesn't change.

    Net effect: production traffic now flows through V2 persistence.
    """
    user_message = body.get("user_message") or body.get("message") or ""
    orchestrator = getattr(harness, "orchestrator", None)
    runtime = getattr(harness, "runtime", None)
    if orchestrator is None or not hasattr(orchestrator, "stream_chat"):
        yield ("error", {"reason": "no orchestrator"})
        return

    # V2: persist a run for this turn. Failures are non-fatal — chat
    # should still work even if the runtime DB is locked or schema is
    # behind.
    run_id: str | None = None
    turn_id: str | None = None
    if runtime is not None:
        try:
            import uuid as _uuid
            turn_id = body.get("turn_id") or _uuid.uuid4().hex
            if route is None:
                from tradingagents.agent_harness.core.tier import (
                    Intent, Op, Tier, RouteResult as _RouteResult,
                )
                # Use a plain dict (RouteResult.to_dict) since
                # RunRepository.create_run does ``dict(route)`` on the
                # value and dataclasses aren't directly iterable.
                route = _RouteResult(
                    intent=Intent.UNKNOWN,
                    op=Op.READ,
                    tier=Tier.PLAN_EXECUTE,
                    symbols=[],
                    confidence=0.5,
                ).to_dict()
            run = runtime.start_analysis(
                session_id=session_id,
                turn_id=turn_id,
                route=route,
                planner_agent="planner",
                planner_capability="planning",
            )
            run_id = run["run_id"]
        except Exception as exc:
            LOGGER.warning("V2 runtime start_analysis failed (non-fatal): %s", exc)

    try:
        async for event in orchestrator.stream_chat(
            session_id=session_id, user_message=user_message,
        ):
            # Annotate every event with the V2 run_id so the UI / audit
            # log can correlate V1 SSE events with V2 persistence rows.
            if isinstance(event, tuple) and len(event) == 2:
                ev_name, payload = event
                if isinstance(payload, dict) and run_id and "v2_run_id" not in payload:
                    payload = {**payload, "v2_run_id": run_id}
                yield ev_name, payload
            elif isinstance(event, dict):
                yield ("error", event)
            else:
                yield event
    except Exception as exc:
        LOGGER.exception("chat_handler failed: %s", exc)
        yield ("error", {"reason": str(exc)})
    finally:
        if runtime is not None and run_id is not None:
            try:
                runtime.resume(run_id=run_id)
            except Exception as exc:
                LOGGER.debug("V2 runtime resume noop (non-fatal): %s", exc)


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
