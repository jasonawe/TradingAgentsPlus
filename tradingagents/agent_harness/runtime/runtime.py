"""Task 12 — AgentRuntime facade.

Public lifecycle surface:
- start_analysis: create AGENT_ANALYSIS run + planner task
- start_command: create SYSTEM_COMMAND run + command task
- stream: read durable events and append connection-only control events
- resume: re-activate a paused run (after recover / explicit resume)
- answer: deliver user ANSWER for a WAITING_MESSAGE task
- confirm: deliver user APPROVAL for a WAITING_APPROVAL task
- cancel: mark run CANCELLED and propagate to RUNNING tasks
- reconcile: handle INDETERMINATE tasks (CONFIRMED_EXECUTED / _NOT_EXECUTED)
- recover: scan + re-aggregate after a crash
- delete_session: remove all runs + tasks + events for a session

Compose store / policy / scheduler / dispatcher / outbox without adding
business prompts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


SchedulerFactory = Callable[[Any], Any]


def _now_iso(now: str | None = None) -> str:
    return now or datetime.now(timezone.utc).isoformat()


# ════════════════════════════════════════════════════════
# AgentRuntime
# ════════════════════════════════════════════════════════


class AgentRuntime:
    """Facade composing store / policy / scheduler / dispatcher / outbox.

    Constructor takes all collaborators explicitly — no global state, no
    hidden service locators.  Tests inject fakes for agent_registry,
    command_resolver, message_ingestor, context_provider, and scheduler.
    """

    def __init__(
        self,
        *,
        store: Any,
        agent_registry: Any,
        command_resolver: Any,
        message_ingestor: Any,
        context_provider: Any,
        scheduler_factory: SchedulerFactory | None = None,
        policy_guard: Any | None = None,
    ) -> None:
        self.store = store
        self.agent_registry = agent_registry
        self.command_resolver = command_resolver
        self.message_ingestor = message_ingestor
        self.context_provider = context_provider
        self._scheduler_factory = scheduler_factory or (
            lambda s: __import__(
                "tradingagents.agent_harness.runtime.scheduler",
                fromlist=["TaskScheduler"],
            ).TaskScheduler(s)
        )
        self.policy_guard = policy_guard
        # Lazy dispatcher — created lazily because tests construct the
        # runtime before the dispatcher import path is fully wired.
        self._dispatcher: Any = None

    # ─── dispatcher accessor ────────────────────────────────

    @property
    def dispatcher(self) -> Any:
        if self._dispatcher is None:
            from .dispatcher import AgentDispatcher

            self._dispatcher = AgentDispatcher(
                agent_registry=self.agent_registry,
                command_resolver=self.command_resolver,
                message_ingestor=self.message_ingestor,
                context_provider=self.context_provider,
            )
        return self._dispatcher

    # ─── run creation ───────────────────────────────────────

    def start_analysis(
        self,
        *,
        session_id: str,
        turn_id: str,
        route: Any,
        planner_agent: str,
        planner_capability: str,
        planner_objective: str = "plan the analysis",
        now: str | None = None,
    ) -> dict[str, Any]:
        """Create an AGENT_ANALYSIS run with the planner as the first task."""
        return self._start_run(
            session_id=session_id,
            turn_id=turn_id,
            run_kind="AGENT_ANALYSIS",
            route=route,
            first_task_kind="AGENT",
            first_task_agent=planner_agent,
            first_task_capability=planner_capability,
            first_task_objective=planner_objective,
            now=now,
        )

    def start_command(
        self,
        *,
        session_id: str,
        turn_id: str,
        route: Any,
        system_handler: str,
        capability: str,
        objective: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Create a SYSTEM_COMMAND run with a single command task."""
        return self._start_run(
            session_id=session_id,
            turn_id=turn_id,
            run_kind="SYSTEM_COMMAND",
            route=route,
            first_task_kind="SYSTEM_COMMAND",
            first_task_agent=None,
            first_task_capability=capability,
            first_task_objective=objective,
            first_task_system_handler=system_handler,
            now=now,
        )

    def _start_run(
        self,
        *,
        session_id: str,
        turn_id: str,
        run_kind: str,
        route: Any,
        first_task_kind: str,
        first_task_agent: str | None,
        first_task_capability: str,
        first_task_objective: str,
        first_task_system_handler: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        from .persistence.runs import RunRepository
        from .persistence.tasks import TaskRepository

        ts = _now_iso(now)
        rr = RunRepository(self.store)
        run = rr.create_run(
            run_id=str(__import__("uuid").uuid4()),
            session_id=session_id,
            turn_id=turn_id,
            run_kind=run_kind,
            route=route,
            budgets={"max_llm_calls": 10},
            now=ts,
        )

        # Create the first task (planner for AGENT_ANALYSIS, command for
        # SYSTEM_COMMAND).
        tr = TaskRepository(self.store)
        class _P:
            objective = first_task_objective
            inputs = {}
        tr.create_task(
            task_id=str(__import__("uuid").uuid4()),
            run_id=run["run_id"],
            parent_task_id=None,
            kind=first_task_kind,
            agent_name=first_task_agent,
            system_handler=first_task_system_handler,
            capability=first_task_capability,
            payload=_P(),
            required=True,
            max_execution_attempts=3,
            now=ts,
        )
        return run

    # ─── streaming (connection-only control events) ─────────

    def stream(self, run_id: str, *, since_seq: int = 0) -> list[dict[str, Any]]:
        """Return durable events with ``seq > since_seq``.

        The Runtime adds **no business logic** here — it just reads the
        runtime_events table.  Connection-only control events (heartbeats,
        progress, terminal flags) are appended by callers/SSE handlers.
        """
        cur = self.store.connection.execute(
            "SELECT * FROM runtime_events WHERE run_id=? AND seq > ? "
            "ORDER BY seq ASC",
            (run_id, since_seq),
        )
        return [dict(r) for r in cur.fetchall()]

    # ─── resume / answer / confirm ──────────────────────────

    def resume(
        self,
        *,
        run_id: str,
        now: str | None = None,
    ) -> dict[str, list[str]]:
        """Resume a paused run by driving the scheduler until blocked."""
        return self.scheduler.run_until_blocked(run_id, now=now)

    def answer(
        self,
        *,
        run_id: str,
        task_id: str,
        value: Any,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Deliver user ANSWER for a WAITING_MESSAGE task and resume."""
        from .persistence.tasks import TaskRepository

        ts = _now_iso(now)
        tr = TaskRepository(self.store)
        current = tr.get_task(task_id)
        if current is None:
            raise RuntimeError(f"task not found: {task_id}")
        if current["state"] != "WAITING_MESSAGE":
            raise RuntimeError(
                f"task {task_id} not WAITING_MESSAGE (state={current['state']})"
            )
        updated = tr.transition_task(
            task_id=task_id,
            expected_version=current["version"],
            expected_state="WAITING_MESSAGE",
            new_state="READY",
            now=ts,
        )
        # Persist user ANSWER message via ingestor
        from .models import AgentMessageDraft, AnswerPayload

        answer_draft = AgentMessageDraft(
            recipient=current.get("agent_name") or "RuntimeAgent",
            type="ANSWER",
            payload=AnswerPayload(
                kind="ANSWER",
                question_message_id=task_id,
                value=value,
                answered_by="user",
            ),
            evidence_refs=[],
        )
        self.message_ingestor.ingest(
            run_id=run_id,
            turn_id="",
            task={"task_id": task_id, "parent_task_id": current.get("parent_task_id"), "agent_name": current.get("agent_name"), "execution_attempt": 1},
            draft=answer_draft,
            now=ts,
        )
        return updated

    def confirm(
        self,
        *,
        run_id: str,
        operation_id: str,
        approved: bool,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Deliver user approval/rejection for a WAITING_APPROVAL operation."""
        ts = _now_iso(now)
        # Persist decision through ingestor; the actual operation
        # transition is handled by Chunk 3's OperationRepository.
        from .models import AgentMessageDraft, ResultPayload

        confirm_draft = AgentMessageDraft(
            recipient="RuntimeAgent",
            type="RESULT",
            payload=ResultPayload(
                kind="RESULT",
                success=bool(approved),
                content="approved" if approved else "rejected",
                artifact_refs=[],
                confidence=1.0,
                missing_items=[],
                errors=[],
            ),
            evidence_refs=[],
        )
        self.message_ingestor.ingest(
            run_id=run_id,
            turn_id="",
            task={"task_id": operation_id, "parent_task_id": None, "agent_name": None, "execution_attempt": 1},
            draft=confirm_draft,
            now=ts,
        )
        return {"operation_id": operation_id, "approved": approved}

    # ─── cancel / reconcile / recover / delete_session ──────

    def cancel(
        self,
        *,
        run_id: str,
        reason_code: str = "USER_CANCELLED",
        now: str | None = None,
    ) -> dict[str, Any]:
        """Mark run CANCELLED, propagate to RUNNING / READY tasks."""
        ts = _now_iso(now)
        with self.store.serial_write():
            cur = self.store.connection.execute(
                "UPDATE agent_tasks SET state='CANCELLED', version=version+1, "
                "updated_at=? WHERE run_id=? AND state IN "
                "('READY','PLANNED','RUNNING','WAITING_CHILD','WAITING_MESSAGE')",
                (ts, run_id),
            )
            cancelled_tasks = cur.rowcount
            cur = self.store.connection.execute(
                "UPDATE agent_runs SET state='CANCELLED', "
                "terminal_reason=?, version=version+1, updated_at=? "
                "WHERE run_id=? AND state NOT IN "
                "('SUCCEEDED','FAILED','CANCELLED','PARTIAL_SUCCESS','LEGACY_INTERRUPTED')",
                (reason_code, ts, run_id),
            )
            self.store.connection.commit()
        return {"run_id": run_id, "cancelled_tasks": cancelled_tasks}

    def reconcile(
        self,
        *,
        run_id: str,
        task_id: str,
        outcome: str,
        operator: str = "user",
        now: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile an INDETERMINATE task into a terminal state.

        ``outcome`` ∈ {"CONFIRMED_EXECUTED", "CONFIRMED_NOT_EXECUTED"}.
        CONFIRMED_NOT_EXECUTED → task READY (retry); EXECUTED → SUCCEEDED.
        """
        ts = _now_iso(now)
        from .persistence.tasks import TaskRepository

        tr = TaskRepository(self.store)
        current = tr.get_task(task_id)
        if current is None:
            raise RuntimeError(f"task not found: {task_id}")
        if current["state"] != "INDETERMINATE":
            raise RuntimeError(
                f"task {task_id} not INDETERMINATE (state={current['state']})"
            )
        new_state = "READY" if outcome == "CONFIRMED_NOT_EXECUTED" else "SUCCEEDED"
        updated = tr.transition_task(
            task_id=task_id,
            expected_version=current["version"],
            expected_state="INDETERMINATE",
            new_state=new_state,
            now=ts,
        )
        return updated

    def recover(
        self,
        *,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Composite recover: store.recover() + scheduler.run_until_blocked."""
        store_summary = self.store.recover(now=now)
        # Drain all active runs to propagate any state promotions.
        for run in self.store.connection.execute(
            "SELECT run_id FROM agent_runs WHERE state NOT IN "
            "('SUCCEEDED','FAILED','CANCELLED','PARTIAL_SUCCESS','LEGACY_INTERRUPTED')"
        ).fetchall():
            self.scheduler.run_until_blocked(run[0], now=now)
        return store_summary

    def delete_session(
        self,
        *,
        session_id: str,
        now: str | None = None,
    ) -> dict[str, int]:
        """Hard-delete all runs + tasks + events for a session."""
        del_counts = {
            "runs": 0, "tasks": 0, "events": 0,
            "messages": 0, "outbox": 0, "deps": 0, "waits": 0,
        }
        ts = _now_iso(now)
        with self.store.serial_write():
            conn = self.store.connection
            run_rows = conn.execute(
                "SELECT run_id FROM agent_runs WHERE session_id=?",
                (session_id,),
            ).fetchall()
            run_ids = [r[0] for r in run_rows]
            if not run_ids:
                return del_counts
            placeholders = ",".join("?" * len(run_ids))

            cur = conn.execute(
                f"DELETE FROM runtime_events WHERE run_id IN ({placeholders})",
                run_ids,
            )
            del_counts["events"] = cur.rowcount
            cur = conn.execute(
                f"DELETE FROM agent_outbox WHERE run_id IN ({placeholders})",
                run_ids,
            )
            del_counts["outbox"] = cur.rowcount
            cur = conn.execute(
                f"DELETE FROM agent_messages WHERE run_id IN ({placeholders})",
                run_ids,
            )
            del_counts["messages"] = cur.rowcount
            cur = conn.execute(
                f"DELETE FROM agent_task_waits WHERE run_id IN ({placeholders})",
                run_ids,
            )
            del_counts["waits"] = cur.rowcount
            cur = conn.execute(
                f"DELETE FROM agent_task_dependencies WHERE run_id IN ({placeholders})",
                run_ids,
            )
            del_counts["deps"] = cur.rowcount
            cur = conn.execute(
                f"DELETE FROM agent_tasks WHERE run_id IN ({placeholders})",
                run_ids,
            )
            del_counts["tasks"] = cur.rowcount
            cur = conn.execute(
                f"DELETE FROM agent_runs WHERE session_id=?",
                (session_id,),
            )
            del_counts["runs"] = cur.rowcount
            conn.commit()
        return del_counts

    # ─── scheduler accessor ─────────────────────────────────

    @property
    def scheduler(self) -> Any:
        if not hasattr(self, "_scheduler") or self._scheduler is None:
            self._scheduler = self._scheduler_factory(self.store)
        return self._scheduler


__all__ = ["AgentRuntime", "SchedulerFactory"]
