"""Task 4 — TaskRepository。

负责:
- create_task(graph insert)
- transition_task(state + version CAS)
- add_graph_patch_child(GraphPatch protocol + bump graph_revision)
- create_wait / resolve_wait / get_wait
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any


class TaskRepository:
    def __init__(self, store):
        self._store = store

    @property
    def conn(self) -> sqlite3.Connection:
        return self._store.connection

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for k in ("inputs_json", "result_json", "error_json"):
            if k in d and d[k] is not None:
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def create_task(
        self,
        *,
        task_id: str,
        run_id: str,
        parent_task_id: str | None,
        kind: str,
        agent_name: str | None,
        system_handler: str | None,
        capability: str | None,
        payload: Any,
        required: bool,
        max_execution_attempts: int,
        now: str,
    ) -> dict[str, Any]:
        try:
            self.conn.execute(
                """
                INSERT INTO agent_tasks
                  (task_id, run_id, parent_task_id, kind, agent_name,
                   system_handler, capability, objective, inputs_json,
                   required, state, max_execution_attempts,
                   version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, 0, ?, ?)
                """,
                (
                    task_id, run_id, parent_task_id, kind, agent_name,
                    system_handler, capability,
                    payload.objective if hasattr(payload, "objective") else "",
                    json.dumps(payload.inputs if hasattr(payload, "inputs") else {}),
                    1 if required else 0,
                    max_execution_attempts, now, now,
                ),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise RuntimeError(f"failed to create task {task_id}: {e}") from e
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)
        )
        row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def transition_task(
        self,
        *,
        task_id: str,
        expected_version: int,
        expected_state: str,
        new_state: str,
        now: str,
    ) -> dict[str, Any]:
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_tasks
                SET state = ?, version = version + 1, updated_at = ?
                WHERE task_id = ? AND state = ? AND version = ?
                """,
                (new_state, now, task_id, expected_state, expected_version),
            )
            if cur.rowcount != 1:
                current = self.get_task(task_id)
                cur_v = current["version"] if current else "?"
                cur_s = current["state"] if current else "?"
                raise RuntimeError(
                    f"task {task_id} CAS failed: "
                    f"expected (state={expected_state}, version={expected_version}), "
                    f"got (state={cur_s}, version={cur_v})"
                )
            self.conn.commit()
        return self.get_task(task_id)

    def add_graph_patch_child(
        self,
        *,
        run_id: str,
        parent_task_id: str,
        kind: str,
        agent_name: str | None,
        capability: str | None,
        payload_objective: str,
        payload_inputs: dict,
        expected_graph_revision: int,
        now: str,
    ) -> dict[str, Any]:
        # GraphPatch 原子协议:
        # 1. bump_graph_revision(CAS)— 拿到新 revision
        # 2. insert child task
        # 失败 → ROLLBACK
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "UPDATE agent_runs SET graph_revision = graph_revision + 1, "
                "version = version + 1, updated_at = ? "
                "WHERE run_id = ? AND graph_revision = ?",
                (now, run_id, expected_graph_revision),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"graph_revision CAS failed for run {run_id} "
                    f"(expected {expected_graph_revision})"
                )
            task_id = str(uuid.uuid4())
            self.conn.execute(
                """
                INSERT INTO agent_tasks
                  (task_id, run_id, parent_task_id, kind, agent_name,
                   capability, objective, inputs_json,
                   required, state, max_execution_attempts,
                   version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', 3, 0, ?, ?)
                """,
                (
                    task_id, run_id, parent_task_id, kind, agent_name,
                    capability, payload_objective,
                    json.dumps(payload_inputs),
                    1, now, now,
                ),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get_task(task_id)

    def create_wait(
        self,
        *,
        run_id: str,
        waiter_task_id: str,
        child_task_id: str,
        wait_kind: str,
        failure_policy: str,
        now: str,
    ) -> dict[str, Any]:
        wait_id = str(uuid.uuid4())
        try:
            self.conn.execute(
                """
                INSERT INTO agent_task_waits
                  (wait_id, run_id, waiter_task_id, child_task_id,
                   wait_kind, failure_policy, state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'WAITING', ?)
                """,
                (wait_id, run_id, waiter_task_id, child_task_id,
                 wait_kind, failure_policy, now),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise RuntimeError(f"failed to create wait: {e}") from e
        return self.get_wait(wait_id)

    def get_wait(self, wait_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM agent_task_waits WHERE wait_id = ?", (wait_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def resolve_wait(
        self,
        *,
        wait_id: str,
        child_state: str,
        now: str,
    ) -> dict[str, Any]:
        new_state = "RESOLVED"
        cur = self.conn.execute(
            """
            UPDATE agent_task_waits
            SET state = ?, resolved_at = ?
            WHERE wait_id = ? AND state = 'WAITING'
            """,
            (new_state, now, wait_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"wait {wait_id} resolve CAS failed")
        self.conn.commit()
        return self.get_wait(wait_id)


__all__ = ["TaskRepository"]
