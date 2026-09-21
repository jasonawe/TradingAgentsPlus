"""Task 9 — UsageReservationRepository。

负责:
- reserve(atomic CAS budget enforcement — 超额抛 BudgetExceeded)
- settle(actual_input / actual_output tokens)
- release(provider 抛错前 / cache hit 时释放预留)
- expire(lease 过期回收)
- usage_summary_by_agent(run_id) — 给 Planner 算 budget

reservation 状态机:
  RESERVED → SETTLED (成功)
  RESERVED → RELEASED (pre-send 失败 / cache hit)
  RESERVED → EXPIRED_COMMITTED (lease 过期但已发起 provider call)
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any


class BudgetExceeded(RuntimeError):
    """run 预算已耗尽 — caller 应放弃后续 LLM 调用。"""


class UsageReservationRepository:
    def __init__(self, store):
        self._store = store

    @property
    def conn(self) -> sqlite3.Connection:
        return self._store.connection

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    # ------------------------------------------------------------------
    # reserve — 原子 CAS,超额抛错
    # ------------------------------------------------------------------

    def reserve(
        self,
        *,
        reservation_id: str,
        call_id: str,
        run_id: str,
        task_id: str,
        agent_name: str,
        execution_attempt: int,
        call_ordinal: int,
        provider: str,
        model: str,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        lease_expires_at: str,
        now: str,
        state: str = "RESERVED",
    ) -> dict[str, Any]:
        """原子地插入 reservation + 检查 budget 不超额。

        超额时抛 ``BudgetExceeded``;之前的预留不变化。
        """
        with self._store.serial_write():
            # 当前预留总和
            cur = self.conn.execute(
                """
                SELECT COALESCE(SUM(reserved_input_tokens + reserved_output_tokens), 0) AS used
                FROM agent_usage_reservations
                WHERE run_id = ? AND state IN ('RESERVED', 'SETTLED', 'EXPIRED_COMMITTED')
                """,
                (run_id,),
            )
            used = int(cur.fetchone()["used"])
            # 找 run 的 max_tokens budget
            budget_row = self.conn.execute(
                "SELECT budgets_json FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            max_tokens = 0
            if budget_row:
                import json
                try:
                    b = json.loads(budget_row["budgets_json"])
                    max_tokens = int(b.get("max_tokens", 0))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            cost = reserved_input_tokens + reserved_output_tokens
            if max_tokens > 0 and used + cost > max_tokens:
                raise BudgetExceeded(
                    f"run {run_id} budget exhausted: used={used}, "
                    f"requested={cost}, max={max_tokens}"
                )
            self.conn.execute(
                """
                INSERT INTO agent_usage_reservations
                  (reservation_id, call_id, run_id, task_id, agent_name,
                   execution_attempt, call_ordinal, provider, model, state,
                   reserved_input_tokens, reserved_output_tokens,
                   provider_attempt_count, lease_expires_at,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    reservation_id, call_id, run_id, task_id, agent_name,
                    execution_attempt, call_ordinal, provider, model, state,
                    reserved_input_tokens, reserved_output_tokens,
                    lease_expires_at, now, now,
                ),
            )
            self.conn.commit()
        return self._row(reservation_id)

    def settle(
        self, *, reservation_id: str,
        actual_input_tokens: int, actual_output_tokens: int, now: str,
    ) -> dict[str, Any]:
        """RESERVED → SETTLED,写入 actual tokens。"""
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_usage_reservations
                SET state = 'SETTLED',
                    actual_input_tokens = ?, actual_output_tokens = ?,
                    updated_at = ?
                WHERE reservation_id = ? AND state = 'RESERVED'
                """,
                (actual_input_tokens, actual_output_tokens, now, reservation_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"reservation {reservation_id} settle CAS failed")
            self.conn.commit()
        return self._row(reservation_id)

    def release(self, *, reservation_id: str, now: str) -> dict[str, Any]:
        """RESERVED → RELEASED — 取消预留。"""
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_usage_reservations
                SET state = 'RELEASED', updated_at = ?
                WHERE reservation_id = ? AND state = 'RESERVED'
                """,
                (now, reservation_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"reservation {reservation_id} release CAS failed")
            self.conn.commit()
        return self._row(reservation_id)

    def expire_stale(self, *, now: str) -> int:
        """Lease 过期且仍 RESERVED → EXPIRED_COMMITTED(按预留量计费)。"""
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_usage_reservations
                SET state = 'EXPIRED_COMMITTED',
                    actual_input_tokens = reserved_input_tokens,
                    actual_output_tokens = reserved_output_tokens,
                    updated_at = ?
                WHERE state = 'RESERVED'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (now, now),
            )
            self.conn.commit()
        return cur.rowcount

    def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM agent_usage_reservations WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        )
        return [self._row_to_dict(r) for r in cur.fetchall()]

    def usage_summary_by_agent(self, run_id: str) -> list[dict[str, Any]]:
        """按 agent 聚合 usage — Planner 用。"""
        cur = self.conn.execute(
            """
            SELECT agent_name,
                   COUNT(*) AS call_count,
                   COALESCE(SUM(COALESCE(actual_input_tokens, reserved_input_tokens)), 0)
                       AS total_input_tokens,
                   COALESCE(SUM(COALESCE(actual_output_tokens, reserved_output_tokens)), 0)
                       AS total_output_tokens
            FROM agent_usage_reservations
            WHERE run_id = ?
              AND state IN ('SETTLED', 'EXPIRED_COMMITTED')
            GROUP BY agent_name
            """,
            (run_id,),
        )
        return [self._row_to_dict(r) for r in cur.fetchall()]

    def _row(self, reservation_id: str) -> dict[str, Any]:
        cur = self.conn.execute(
            "SELECT * FROM agent_usage_reservations WHERE reservation_id = ?",
            (reservation_id,),
        )
        row = cur.fetchone()
        return self._row_to_dict(row) if row else {}


__all__ = ["UsageReservationRepository", "BudgetExceeded"]
