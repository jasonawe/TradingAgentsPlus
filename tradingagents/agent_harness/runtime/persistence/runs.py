"""Task 4 — RunRepository。

负责:
- create_run(active 唯一性,同 session 不能并发 active)
- bump_graph_revision(graph revision CAS)
- mark_terminal(终态 + terminal_seq)
- list_active_runs / list_runs(by kind) / get_run
- get_latest_recoverable_run(per session)
- register_legacy_replacement(唯一性)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class RunRepository:
    ACTIVE_STATES = ("PLANNING", "RUNNING", "WAITING_USER",
                     "WAITING_APPROVAL", "NEEDS_RECONCILIATION")
    TERMINAL_STATES = ("SUCCEEDED", "PARTIAL_SUCCESS", "FAILED",
                       "CANCELLED", "LEGACY_INTERRUPTED")

    def __init__(self, store):
        self._store = store

    @property
    def conn(self) -> sqlite3.Connection:
        return self._store.connection

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        # 解 JSON 列并重命名为不带 _json 后缀的 key
        for json_col, alias in (
            ("route_json", "route"),
            ("budgets_json", "budgets"),
            ("final_result_json", "final_result"),
        ):
            if json_col in d and d[json_col] is not None:
                try:
                    d[alias] = json.loads(d[json_col])
                except (json.JSONDecodeError, TypeError):
                    d[alias] = d[json_col]
                # 保留 _json 列,供调试
        return d

    def create_run(
        self,
        *,
        run_id: str,
        session_id: str,
        turn_id: str,
        run_kind: str,
        route: Any,
        budgets: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        try:
            cur = self.conn.execute(
                """
                INSERT INTO agent_runs
                  (run_id, session_id, turn_id, run_kind, state,
                   route_json, budgets_json, next_seq, graph_revision, version,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, 'PLANNING', ?, ?, 1, 0, 0, ?, ?)
                """,
                (
                    run_id, session_id, turn_id, run_kind,
                    json.dumps(route.model_dump() if hasattr(route, "model_dump") else dict(route)),
                    json.dumps(budgets),
                    now, now,
                ),
            )
            return self.get_run(run_id)
        except sqlite3.IntegrityError as e:
            raise RuntimeError(
                f"active run already exists for session {session_id}"
            ) from e

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
        )
        row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def list_runs(self, *, run_kind: str | None = None,
                  limit: int = 100) -> list[dict[str, Any]]:
        if run_kind is not None:
            cur = self.conn.execute(
                "SELECT * FROM agent_runs WHERE run_kind = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (run_kind, limit),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [self._row_to_dict(r) for r in cur.fetchall()]

    def list_active_runs(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(self.ACTIVE_STATES))
        cur = self.conn.execute(
            f"SELECT * FROM agent_runs WHERE state IN ({placeholders}) "
            f"ORDER BY created_at DESC",
            self.ACTIVE_STATES,
        )
        return [self._row_to_dict(r) for r in cur.fetchall()]

    def get_latest_recoverable_run(self, session_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            """
            SELECT * FROM agent_runs
            WHERE session_id = ?
              AND state IN ('PLANNING', 'RUNNING', 'WAITING_USER',
                            'WAITING_APPROVAL', 'NEEDS_RECONCILIATION', 'FAILED', 'PARTIAL_SUCCESS')
            ORDER BY created_at DESC LIMIT 1
            """,
            (session_id,),
        )
        row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def bump_graph_revision(self, *, run_id: str, expected_revision: int, now: str) -> int:
        cur = self.conn.execute(
            "UPDATE agent_runs SET graph_revision = graph_revision + 1, "
            "version = version + 1, updated_at = ? "
            "WHERE run_id = ? AND graph_revision = ?",
            (now, run_id, expected_revision),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"graph_revision CAS failed for run {run_id} "
                f"(expected {expected_revision})"
            )
        self.conn.commit()
        return expected_revision + 1

    def mark_terminal(
        self,
        *,
        run_id: str,
        expected_state: str,
        final_seq: int,
        final_result_json: Any,
        terminal_reason: str,
        now: str,
    ) -> dict[str, Any]:
        # terminal_reason 是 run 的终止原因;state 列固定映射到规范终态。
        # 规范终态直接落地;描述性原因(如 OUTBOX_SCHEDULER_DEAD)映射到 FAILED。
        CANONICAL_TERMINAL_STATES = {
            "SUCCEEDED", "PARTIAL_SUCCESS", "FAILED",
            "CANCELLED", "LEGACY_INTERRUPTED",
        }
        REASON_TO_STATE = {
            "COMPLETED": "SUCCEEDED",
            "OUTBOX_SCHEDULER_DEAD": "FAILED",
            "USER_CANCELLED": "CANCELLED",
        }
        if terminal_reason in CANONICAL_TERMINAL_STATES:
            final_state = terminal_reason
        else:
            final_state = REASON_TO_STATE.get(terminal_reason, "FAILED")
        cur = self.conn.execute(
            """
            UPDATE agent_runs
            SET state = ?, terminal_seq = ?, terminal_reason = ?,
                final_result_json = ?, version = version + 1, updated_at = ?
            WHERE run_id = ? AND state = ?
            """,
            (
                final_state, final_seq, terminal_reason,
                json.dumps(final_result_json) if final_result_json is not None else None,
                now, run_id, expected_state,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"terminal CAS failed for run {run_id} (expected state {expected_state})"
            )
        self.conn.commit()
        return self.get_run(run_id)

    def register_legacy_replacement(
        self,
        *,
        legacy_store_identity: str,
        session_id: str,
        workflow_name: str | None,
        milestone_id: str,
        node_position: str,
        original_user_message: str | None,
        state: str,
        replacement_run_id: str,
        now: str,
    ) -> None:
        # migration_key 是 legacy slot 的业务主键,不包含 session_id —
        # 跨 session 也不能为同一 workflow/milestone/node_position 注册多次
        migration_key = (
            f"{legacy_store_identity}|{workflow_name or ''}|"
            f"{milestone_id}|{node_position}"
        )
        try:
            self.conn.execute(
                """
                INSERT INTO agent_legacy_interruptions
                  (migration_key, legacy_store_identity, session_id, workflow_name,
                   milestone_id, node_position, original_user_message, state,
                   replacement_run_id, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    migration_key, legacy_store_identity, session_id,
                    workflow_name, milestone_id, node_position,
                    original_user_message, state, replacement_run_id, now,
                ),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise RuntimeError(
                f"legacy replacement already registered: {migration_key}"
            ) from e


__all__ = ["RunRepository"]
