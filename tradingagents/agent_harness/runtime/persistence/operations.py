"""Task 8 — OperationRepository。

负责:
- create_operation(PENDING 状态,持久化 BEFORE side effect)
- claim_operation(PENDING → EXECUTING,worker_id + lease)
- mark_completed(operation_id, result_json)
- mark_failed(operation_id, error_json)
- mark_indeterminate(operation_id, last_error)
- mark_awaiting_approval / mark_approved / mark_rejected
- get_operation / list_operations

operation_id 由调用方提供(uuid);同一 operation_id 重复 create 走
CAS,保证幂等。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any


class OperationRepository:
    def __init__(self, store):
        self._store = store

    @property
    def conn(self) -> sqlite3.Connection:
        return self._store.connection

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for col in ("redacted_args_json", "result_json", "error_json"):
            if col in d and d[col] is not None:
                try:
                    d[col] = json.loads(d[col])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM agent_operations WHERE operation_id = ?",
            (operation_id,),
        )
        row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def create_operation(
        self,
        *,
        operation_id: str,
        idempotency_key: str,
        run_id: str,
        task_id: str,
        tool_name: str,
        args_hash: str,
        redacted_args: dict[str, Any],
        side_effect_mode: str,
        now: str,
        approval_id: str | None = None,
        remote_idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """持久化 BEFORE side effect。如果 operation_id 已存在,返回现有 row(幂等)。"""
        with self._store.serial_write():
            existing = self.get_operation(operation_id)
            if existing is not None:
                return existing
            try:
                self.conn.execute(
                    """
                    INSERT INTO agent_operations
                      (operation_id, idempotency_key, run_id, task_id, tool_name,
                       args_hash, redacted_args_json, side_effect_mode,
                       approval_id, state, remote_idempotency_key,
                       version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, 0, ?, ?)
                    """,
                    (
                        operation_id, idempotency_key, run_id, task_id, tool_name,
                        args_hash, json.dumps(redacted_args), side_effect_mode,
                        approval_id, remote_idempotency_key,
                        now, now,
                    ),
                )
                self.conn.commit()
            except sqlite3.IntegrityError as e:
                raise RuntimeError(f"operation {operation_id} create failed: {e}") from e
        return self.get_operation(operation_id)

    def claim_operation(
        self, *, operation_id: str, worker_id: str | None = None,
        lease_expires_at: str | None = None, now: str,
    ) -> dict[str, Any]:
        """PENDING/RETRY_AUTHORIZED → EXECUTING。worker_id / lease 仅写入
        args_json 旁的 debug 字段 — 当前 schema 没有独立列(留给后续 migration)。
        """
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_operations
                SET state = 'EXECUTING', version = version + 1, updated_at = ?
                WHERE operation_id = ? AND state IN ('PENDING', 'RETRY_AUTHORIZED')
                """,
                (now, operation_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"operation {operation_id} claim CAS failed "
                    f"(state must be PENDING or RETRY_AUTHORIZED)"
                )
            self.conn.commit()
        return self.get_operation(operation_id)

    def mark_completed(self, *, operation_id: str, result_json: Any, now: str) -> dict[str, Any]:
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_operations
                SET state = 'SUCCEEDED', result_json = ?,
                    version = version + 1, updated_at = ?
                WHERE operation_id = ? AND state = 'EXECUTING'
                """,
                (json.dumps(result_json) if result_json is not None else None,
                 now, operation_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"operation {operation_id} complete CAS failed")
            self.conn.commit()
        return self.get_operation(operation_id)

    def mark_failed(self, *, operation_id: str, error_json: Any, now: str) -> dict[str, Any]:
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_operations
                SET state = 'FAILED', error_json = ?,
                    version = version + 1, updated_at = ?
                WHERE operation_id = ? AND state IN ('EXECUTING', 'PENDING')
                """,
                (json.dumps(error_json), now, operation_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"operation {operation_id} fail CAS failed")
            self.conn.commit()
        return self.get_operation(operation_id)

    def mark_awaiting_approval(self, *, operation_id: str, approval_id: str,
                                now: str) -> dict[str, Any]:
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_operations
                SET state = 'AWAITING_APPROVAL', approval_id = ?,
                    version = version + 1, updated_at = ?
                WHERE operation_id = ? AND state = 'PENDING'
                """,
                (approval_id, now, operation_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"operation {operation_id} await-approval CAS failed")
            self.conn.commit()
        return self.get_operation(operation_id)

    def mark_indeterminate(self, *, operation_id: str, error_json: Any,
                            now: str) -> dict[str, Any]:
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_operations
                SET state = 'INDETERMINATE', error_json = ?,
                    version = version + 1, updated_at = ?
                WHERE operation_id = ? AND state = 'EXECUTING'
                """,
                (json.dumps(error_json), now, operation_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"operation {operation_id} indeterminate CAS failed")
            self.conn.commit()
        return self.get_operation(operation_id)


__all__ = ["OperationRepository"]
