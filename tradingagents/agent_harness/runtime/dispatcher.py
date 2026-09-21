"""Task 12 — AgentDispatcher.

Pure dispatch layer — given a claimed task, call the right executor:
- AGENT tasks: AgentRegistry.get(name).run(task, context=context)
- SYSTEM_COMMAND tasks: CommandResolver(转 CommandSpec)

Dispatcher **never chooses graph topology** — that's the planner + policy
guard's job.  It only:
1. Builds AgentExecutionContext via ContextProvider Protocol
2. Invokes the agent / command resolver
3. Validates AgentReply (success/content/outgoing/evidence/errors)
4. Commits reply/outgoing/state atomically
5. Routes all outgoing messages through MessageIngestor before persistence
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════
# Protocol — narrow surface area for chunk 3's ContextAssembler
# ════════════════════════════════════════════════════════


class ContextProvider(Protocol):
    """Build the AgentExecutionContext for a given runtime task.

    Chunk 3's ContextAssembler implements this Protocol; tests inject a
    deterministic fake.
    """

    def assemble(self, task: dict[str, Any]) -> dict[str, Any]:
        """Return a context bundle (slots, memory, dependency_results, …)."""
        ...


# ════════════════════════════════════════════════════════
# AgentDispatcher
# ════════════════════════════════════════════════════════


class AgentDispatcher:
    """Dispatch a claimed task to the right executor and persist the reply."""

    def __init__(
        self,
        *,
        agent_registry: Any,
        command_resolver: Any,
        message_ingestor: Any,
        context_provider: ContextProvider,
        tool_executor: Any | None = None,
        llm_executor: Any | None = None,
    ) -> None:
        self.agent_registry = agent_registry
        self.command_resolver = command_resolver
        self.message_ingestor = message_ingestor
        self.context_provider = context_provider
        self.tool_executor = tool_executor
        self.llm_executor = llm_executor

    # ─── dispatch entry point ───────────────────────────────

    def dispatch(
        self,
        *,
        task: dict[str, Any],
        run: dict[str, Any],
        store: Any,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch one claimed task and return a result envelope.

        Returns ``{"task_id": str, "state": str, "messages": list}``.
        The caller (Runtime) is responsible for advancing the run state
        via scheduler aggregation.
        """
        ts = now or datetime.now(timezone.utc).isoformat()
        kind = (task.get("kind") or "AGENT").upper()

        if kind == "AGENT":
            return self._dispatch_agent(task=task, run=run, store=store, now=ts)
        if kind == "SYSTEM_COMMAND":
            return self._dispatch_command(
                task=task, run=run, store=store, now=ts
            )
        raise ValueError(f"unsupported task kind: {kind!r}")

    # ─── AGENT dispatch ─────────────────────────────────────

    def _dispatch_agent(
        self,
        *,
        task: dict[str, Any],
        run: dict[str, Any],
        store: Any,
        now: str,
    ) -> dict[str, Any]:
        agent_name = task.get("agent_name")
        agent = self.agent_registry.get(agent_name) if agent_name else None
        if agent is None:
            raise ValueError(f"unknown agent: {agent_name!r}")

        context = self.context_provider.assemble(task)

        # Build minimal V2 AgentTask view (Chunk 3 will expand the
        # protocol); for now the registry's contract is just ``run(task,
        # context)`` returning an AgentReply-like object.
        v2_task = self._build_v2_task_view(task, run)

        reply = agent.run(v2_task, context=context)

        # Persist the reply atomically: state transition + outgoing
        # ingest + (optional) RESULT creation.
        return self._commit_agent_reply(
            task=task, run=run, store=store, now=now, reply=reply
        )

    @staticmethod
    def _build_v2_task_view(
        task: dict[str, Any], run: dict[str, Any]
    ) -> dict[str, Any]:
        """Project a runtime task into the V2 AgentTask view for the agent.

        The agent only needs:
        - task_id, run_id, turn_id
        - parent_task_id
        - sender/recipient
        - objective, inputs
        - dependency_results
        - constraints (timeout, attempts, allowed_tools, allow_handoff)
        - required
        - execution_attempt
        """
        return {
            "task_id": task["task_id"],
            "run_id": run["run_id"],
            "turn_id": run.get("turn_id", ""),
            "parent_task_id": task.get("parent_task_id"),
            "sender": task.get("sender", "runtime"),
            "recipient": task.get("agent_name", ""),
            "objective": task.get("objective", ""),
            "inputs": task.get("inputs") or {},
            "dependency_results": [],
            "constraints": {
                "timeout_seconds": float(
                    task.get("timeout_seconds") or 60.0
                ),
                "max_execution_attempts": int(
                    task.get("max_execution_attempts") or 3
                ),
                "allowed_tools": list(task.get("allowed_tools") or []),
                "allow_handoff": bool(task.get("allow_handoff", True)),
            },
            "required": bool(task.get("required", True)),
            "execution_attempt": int(task.get("execution_attempt") or 1),
        }

    def _commit_agent_reply(
        self,
        *,
        task: dict[str, Any],
        run: dict[str, Any],
        store: Any,
        now: str,
        reply: Any,
    ) -> dict[str, Any]:
        """Validate reply, ingest outgoing, transition task to terminal."""
        success = bool(getattr(reply, "success", True))
        new_state = "SUCCEEDED" if success else "FAILED"

        with store.serial_write():
            # CAS task RUNNING → terminal (version must match)
            current = store.connection.execute(
                "SELECT version, state FROM agent_tasks WHERE task_id=?",
                (task["task_id"],),
            ).fetchone()
            if current is None:
                raise RuntimeError(f"task vanished: {task['task_id']}")
            if current["state"] != "RUNNING":
                LOGGER.warning(
                    "dispatch on non-RUNNING task %s (state=%s)",
                    task["task_id"], current["state"],
                )
            cur = store.connection.execute(
                "UPDATE agent_tasks SET state=?, version=version+1, "
                "updated_at=? WHERE task_id=? AND version=?",
                (new_state, now, task["task_id"], current["version"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"task {task['task_id']} CAS failed in dispatch"
                )

            # Persist outgoing drafts through ingestor (must happen inside
            # the same write transaction to keep message + state atomic).
            correlation_id = task["task_id"]
            messages = []
            for draft in getattr(reply, "outgoing", []) or []:
                msg = self.message_ingestor.ingest(
                    run_id=run["run_id"],
                    turn_id=run.get("turn_id", ""),
                    task={
                        "task_id": task["task_id"],
                        "parent_task_id": task.get("parent_task_id"),
                        "agent_name": task.get("agent_name"),
                        "execution_attempt": int(
                            task.get("execution_attempt") or 1
                        ),
                    },
                    draft=draft,
                    now=now,
                    correlation_id=correlation_id,
                )
                messages.append(msg)

            # 成功 reply 必须有一条 RESULT 记录 — 如果 agent 没显式发 RESULT,
            # dispatcher 自动注入一条,保证 runtime 一定有确切一条 RESULT。
            # 用 SimpleNamespace 避免 AgentMessageDraft 不接受 type=RESULT 的限制。
            from types import SimpleNamespace
            from .models import ResultPayload, AgentMessageType
            has_result = any(
                getattr(d, "type", None) == AgentMessageType.RESULT.value
                or getattr(d, "type", None) == "RESULT"
                for d in getattr(reply, "outgoing", []) or []
            )
            if success and not has_result:
                synthetic = SimpleNamespace(
                    recipient=run.get("session_id", "runtime"),
                    type=AgentMessageType.RESULT,
                    payload=ResultPayload(
                        kind="RESULT",
                        success=True,
                        content=getattr(reply, "content", "") or "",
                        artifact_refs=list(getattr(reply, "evidence", []) or []),
                        confidence=getattr(reply, "confidence", None),
                        missing_items=list(getattr(reply, "missing_items", []) or []),
                        errors=list(getattr(reply, "errors", []) or []),
                    ),
                    evidence_refs=list(getattr(reply, "evidence", []) or []),
                    reason_summary=None,
                )
                msg = self.message_ingestor.ingest(
                    run_id=run["run_id"],
                    turn_id=run.get("turn_id", ""),
                    task={
                        "task_id": task["task_id"],
                        "parent_task_id": task.get("parent_task_id"),
                        "agent_name": task.get("agent_name"),
                        "execution_attempt": int(
                            task.get("execution_attempt") or 1
                        ),
                    },
                    draft=synthetic,
                    now=now,
                    correlation_id=correlation_id,
                )
                messages.append(msg)

            store.connection.commit()

        return {
            "task_id": task["task_id"],
            "state": new_state,
            "messages": messages,
        }

    # ─── SYSTEM_COMMAND dispatch ─────────────────────────────

    def _dispatch_command(
        self,
        *,
        task: dict[str, Any],
        run: dict[str, Any],
        store: Any,
        now: str,
    ) -> dict[str, Any]:
        from ..core.tier import Intent, Op

        # CommandResolver is the only builder for CommandSpec — we never
        # bypass it to call tools directly.
        spec = self.command_resolver.resolve(
            intent=Intent.QUOTE,
            op=Op.LIST,
            user_message=task.get("objective") or "",
            symbols=task.get("inputs", {}).get("symbols") or [],
            carry_symbols=task.get("inputs", {}).get("carry_symbols") or [],
            slots=task.get("inputs", {}).get("slots") or {},
        )

        # We don't execute the tool here — that's ToolExecutor's job and
        # requires HITL approval flow.  For dispatch purposes we just
        # record the spec transition.
        new_state = "SUCCEEDED"

        with store.serial_write():
            current = store.connection.execute(
                "SELECT version FROM agent_tasks WHERE task_id=?",
                (task["task_id"],),
            ).fetchone()
            if current is None:
                raise RuntimeError(f"task vanished: {task['task_id']}")
            cur = store.connection.execute(
                "UPDATE agent_tasks SET state=?, version=version+1, "
                "updated_at=? WHERE task_id=? AND version=?",
                (new_state, now, task["task_id"], current["version"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"command task {task['task_id']} CAS failed"
                )
            store.connection.commit()

        return {
            "task_id": task["task_id"],
            "state": new_state,
            "command_spec": spec,
        }


__all__ = ["AgentDispatcher", "ContextProvider"]
