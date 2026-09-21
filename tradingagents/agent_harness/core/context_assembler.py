"""Task 14 — ContextAssembler.

Wrap existing ContextPriority providers + resolve immutable dependency
artifacts from RuntimeStore first. Read projection receipts and overlay
only terminal, verified, not-yet-delivered runs.

Returns a typed AgentContextBundle that the V2 dispatcher can hand to the
agent's ``ContextProvider.assemble(task)`` chain.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

LOGGER = logging.getLogger(__name__)


class Layer(IntEnum):
    """Priority ordering for context layers."""

    TASK_INPUT = 1
    DEPENDENCY_ARTIFACTS = 2
    L1_CHAT = 3
    L2_PREFS = 4
    L3_REFS = 5
    SYSTEM_CONSTRAINTS = 6


@dataclass(frozen=True)
class AgentContextBundle:
    """Typed context bundle returned by :class:`ContextAssembler`."""

    task_input: dict[str, Any]
    dependency_artifacts: tuple[dict[str, Any], ...]
    l1_chat: tuple[dict[str, Any], ...]
    l2_prefs: dict[str, Any]
    l3_refs: tuple[dict[str, Any], ...]
    system_constraints: dict[str, Any]

    def layer_order(self) -> list[Layer]:
        return [
            Layer.TASK_INPUT, Layer.DEPENDENCY_ARTIFACTS,
            Layer.L1_CHAT, Layer.L2_PREFS,
            Layer.L3_REFS, Layer.SYSTEM_CONSTRAINTS,
        ]


class ContextAssembler:
    """Build the V2 AgentContextBundle for a claimed task.

    Resolution order:
    1. TASK_INPUT — explicit inputs from the runtime task
    2. DEPENDENCY_ARTIFACTS — verified artifacts from RuntimeStore
       (filtered to non-FAILED, non-INDETERMINATE producers)
    3. L1_CHAT — chat history from MemoryManager, minus any
       already-projected exchange (receipt suppression)
    4. L2_PREFS — user preferences
    5. L3_REFS — long-term references
    6. SYSTEM_CONSTRAINTS — fixed-budget / safety metadata

    Terminal-but-pending Runtime overlay: when a run is in
    NEEDS_RECONCILIATION or WAITING_USER, the bundle carries an
    ``overlay`` hint so the agent can adjust tone.
    """

    def __init__(
        self,
        *,
        store: Any,
        memory: Any | None = None,
        l2_prefs: dict[str, Any] | None = None,
        l3_refs: tuple[dict[str, Any], ...] = (),
        system_constraints: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.memory = memory
        self.l2_prefs = dict(l2_prefs or {})
        self.l3_refs = tuple(l3_refs)
        self.system_constraints = dict(system_constraints or {
            "max_tokens": 4000,
            "deadline_seconds": 600.0,
            "max_tool_calls": 20,
        })

    def assemble(
        self,
        *,
        task: dict[str, Any],
        session_id: str,
    ) -> AgentContextBundle:
        """Resolve all layers for the given task and return a bundle."""
        return AgentContextBundle(
            task_input=self._layer_task_input(task),
            dependency_artifacts=self._layer_dependencies(task),
            l1_chat=self._layer_l1_chat(session_id),
            l2_prefs=dict(self.l2_prefs),
            l3_refs=tuple(self.l3_refs),
            system_constraints=dict(self.system_constraints),
        )

    # ─── layer resolvers ────────────────────────────────────

    @staticmethod
    def _layer_task_input(task: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": task.get("task_id"),
            "run_id": task.get("run_id"),
            "turn_id": task.get("turn_id"),
            "objective": task.get("objective") or "",
            "inputs": dict(task.get("inputs") or {}),
            "symbols": list(task.get("inputs", {}).get("symbols") or []),
            "carry_symbols": list(
                task.get("inputs", {}).get("carry_symbols") or []
            ),
        }

    def _layer_dependencies(
        self, task: dict[str, Any]
    ) -> tuple[dict[str, Any], ...]:
        """Resolve immutable artifacts from agent_artifacts table.

        Skips artifacts whose producer_task is FAILED or INDETERMINATE.
        """
        deps = task.get("dependency_results") or []
        if not deps:
            return ()
        artifacts: list[dict[str, Any]] = []
        for dep in deps:
            producer_task_id = dep.get("producer_task_id")
            artifact_id = dep.get("artifact_id") or ""
            if not producer_task_id:
                continue
            # 跳过 FAILED / INDETERMINATE 的 producer task
            row = self.store.connection.execute(
                "SELECT state FROM agent_tasks WHERE task_id=?",
                (producer_task_id,),
            ).fetchone()
            if row is None:
                continue
            if row[0] in ("FAILED", "INDETERMINATE", "CANCELLED"):
                continue
            # 找对应 artifact
            if artifact_id:
                art_row = self.store.connection.execute(
                    "SELECT * FROM agent_artifacts WHERE run_id=? "
                    "AND producer_task_id=? AND content_sha256=?",
                    (task.get("run_id"), producer_task_id, artifact_id),
                ).fetchone()
            else:
                art_row = self.store.connection.execute(
                    "SELECT * FROM agent_artifacts WHERE run_id=? "
                    "AND producer_task_id=? ORDER BY created_at DESC LIMIT 1",
                    (task.get("run_id"), producer_task_id),
                ).fetchone()
            if art_row is None:
                continue
            d = dict(art_row)
            # 解 JSON content
            try:
                import json
                d["content"] = json.loads(d.get("content_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["content"] = d.get("content_json")
            d.pop("content_json", None)
            artifacts.append(d)
        return tuple(artifacts)

    def _layer_l1_chat(
        self, session_id: str
    ) -> tuple[dict[str, Any], ...]:
        """L1 chat history, projected exchanges filtered out."""
        if self.memory is None:
            return ()
        try:
            history = self.memory.get_history(session_id) or []
        except Exception as e:
            LOGGER.warning("memory.get_history failed: %s", e)
            return ()

        # Find projection keys already applied for this session
        projected: set[str] = set()
        try:
            for entry in self.memory.list_projection_receipts(session_id) or []:
                pk = entry.get("projection_key") if isinstance(entry, dict) else None
                if pk:
                    projected.add(pk)
        except AttributeError:
            # Fallback: query receipts table directly via the memory store
            try:
                receipts = self.memory._db_path  # type: ignore[attr-defined]
            except AttributeError:
                projected = set()
            else:
                import sqlite3
                conn = sqlite3.connect(str(receipts))
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT projection_key FROM runtime_projection_receipts "
                    "WHERE projection_key LIKE ?",
                    (f"runtime:{session_id}:%",),
                )
                for r in cur.fetchall():
                    projected.add(r["projection_key"])
                conn.close()

        # Drop messages whose projection_key appears in receipts.
        # Each history row carries ``role/content/ts``; if content is
        # exactly equal to a projected receipt's user_text, drop it.
        out = []
        for msg in history:
            content = msg.get("content") if isinstance(msg, dict) else None
            if content and f"runtime:{session_id}:{content}" in projected:
                continue
            out.append(msg)
        return tuple(out)

    # ─── pending Runtime overlay ────────────────────────────

    def pending_overlay(
        self, *, run_id: str
    ) -> dict[str, Any] | None:
        """Return overlay hints when a run is in a non-trivial state.

        Used by Runtime.stream to attach terminal state to outgoing
        messages.  Returns ``None`` if the run is in a normal state.
        """
        row = self.store.connection.execute(
            "SELECT state, terminal_reason FROM agent_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        state, reason = row[0], row[1]
        if state == "NEEDS_RECONCILIATION":
            return {"hint": "indeterminate", "reason": reason or ""}
        if state == "WAITING_USER":
            return {"hint": "user_required", "reason": reason or ""}
        if state == "CANCELLED":
            return {"hint": "cancelled", "reason": reason or ""}
        return None


__all__ = [
    "AgentContextBundle",
    "ContextAssembler",
    "Layer",
]
