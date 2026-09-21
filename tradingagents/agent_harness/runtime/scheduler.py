"""Task 11 — TaskScheduler.

Pure store-driven scheduler — 不调用 Agent / Tool / LLM,只读取和更新 store 内的
runtime state(spec §20.3 aggregation)。

主要入口:
- claim_ready_task: version CAS + lease 原子 claim 一个 READY task
- run_once: 一次调度 pass
  1. 解析 ACTIVE wait(等子任务终止)
  2. promote PLANNED task(dependencies 满足)
  3. aggregate run state(SPEC §20.3 优先级)
- run_until_blocked: 循环 run_once 直到没有 state transition
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════
# Terminal states — helpers
# ════════════════════════════════════════════════════════


_TASK_TERMINAL_STATES = ("SUCCEEDED", "FAILED", "CANCELLED", "INDETERMINATE")
_RUN_TERMINAL_STATES = (
    "SUCCEEDED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "LEGACY_INTERRUPTED"
)


def _now_iso(now: str | None = None) -> str:
    return now or datetime.now(timezone.utc).isoformat()


# ════════════════════════════════════════════════════════
# TaskScheduler
# ════════════════════════════════════════════════════════


class TaskScheduler:
    """Pure store-driven scheduler for the supervised AgentRuntime.

    Constructor takes an :class:`AgentRuntimeStore`.  The scheduler never
    imports Agent / Tool / LLM modules — it only reads and writes the
    runtime SQLite tables.
    """

    def __init__(self, store, *, lease_seconds: int = 60) -> None:
        self.store = store
        self.lease_seconds = lease_seconds

    # ─── claim_ready_task ────────────────────────────────────

    def claim_ready_task(
        self,
        *,
        run_id: str | None = None,
        worker_id: str,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim one ``READY`` task with version CAS + lease.

        When ``run_id`` is provided, only READY tasks for that run are
        eligible.  When ``None``, any READY task across all runs is
        eligible (used by global worker pools).
        """
        ts = _now_iso(now)
        lease_until = (
            datetime.fromisoformat(ts) + timedelta(seconds=self.lease_seconds)
        ).isoformat()

        with self.store.serial_write():
            conn = self.store.connection
            if run_id is None:
                cur = conn.execute(
                    "SELECT * FROM agent_tasks WHERE state='READY' "
                    "ORDER BY created_at ASC LIMIT 1"
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM agent_tasks "
                    "WHERE state='READY' AND run_id=? "
                    "ORDER BY created_at ASC LIMIT 1",
                    (run_id,),
                )
            row = cur.fetchone()
            if row is None:
                return None
            task = dict(row)
            cur = conn.execute(
                """
                UPDATE agent_tasks
                SET state='RUNNING', worker_id=?, lease_expires_at=?,
                    version=version+1, updated_at=?
                WHERE task_id=? AND state='READY' AND version=?
                """,
                (worker_id, lease_until, ts, task["task_id"], task["version"]),
            )
            if cur.rowcount != 1:
                return None
            conn.commit()
            # refresh and return
            cur = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=?", (task["task_id"],)
            )
            return dict(cur.fetchone())

    # ─── run_once ────────────────────────────────────────────

    def run_once(
        self, *, run_id: str | None = None, now: str | None = None
    ) -> dict[str, list[str]]:
        """One scheduling pass.

        Returns a report with:
        - ``resolved_wait_ids`` — waits transitioned to ``RESOLVED``
        - ``promoted_task_ids`` — PLANNED tasks transitioned to ``READY``
        - ``aggregated_run_ids`` — runs whose state was updated
        """
        ts = _now_iso(now)
        report = {
            "resolved_wait_ids": [],
            "promoted_task_ids": [],
            "aggregated_run_ids": [],
        }

        with self.store.serial_write():
            conn = self.store.connection

            self._resolve_waits(conn, run_id, ts, report)
            self._promote_planned(conn, run_id, ts, report)
            self._aggregate_runs(conn, run_id, ts, report)
            conn.commit()
        return report

    # ─── run_until_blocked ───────────────────────────────────

    def run_until_blocked(
        self,
        run_id: str,
        *,
        now: str | None = None,
    ) -> dict[str, list[str]]:
        """Loop ``run_once`` until no state transitions happen.

        Used by Runtime to drive a run forward in tests and recovery.
        Bounded to a safety cap so a buggy DAG cannot starve the worker.
        """
        aggregated: dict[str, list[str]] = {
            "resolved_wait_ids": [],
            "promoted_task_ids": [],
            "aggregated_run_ids": [],
        }
        for _ in range(256):
            report = self.run_once(run_id=run_id, now=now)
            if (
                not report["resolved_wait_ids"]
                and not report["promoted_task_ids"]
                and not report["aggregated_run_ids"]
            ):
                break
            aggregated["resolved_wait_ids"].extend(report["resolved_wait_ids"])
            aggregated["promoted_task_ids"].extend(report["promoted_task_ids"])
            aggregated["aggregated_run_ids"].extend(report["aggregated_run_ids"])
        return aggregated

    # ─── internal helpers ────────────────────────────────────

    def _resolve_waits(
        self,
        conn,
        run_id: str | None,
        ts: str,
        report: dict,
    ) -> None:
        """Resolve ``WAITING`` waits whose child reached terminal state.

        Per the test contract:
        - child SUCCEEDED → wait RESOLVED, waiter → READY
        - child FAILED + FAIL_WAITER → wait RESOLVED, waiter → FAILED
        - child FAILED + RESUME_WITH_FAILURE → wait RESOLVED, waiter → READY
        - child CANCELLED → wait RESOLVED, waiter → READY(resume policy
          treated symmetrically to terminal success)
        - child INDETERMINATE → wait stays WAITING(由 reconciliation 决定)
        """
        params: tuple = ()
        if run_id is not None:
            params = (run_id,)
        cur = conn.execute(
            f"""
            SELECT w.wait_id, w.waiter_task_id, w.child_task_id,
                   w.failure_policy, c.state AS child_state,
                   wt.required AS waiter_required,
                   wt.run_id AS waiter_run_id
            FROM agent_task_waits w
            JOIN agent_tasks c ON c.task_id = w.child_task_id
            JOIN agent_tasks wt ON wt.task_id = w.waiter_task_id
            WHERE w.state = 'WAITING'
              {"AND w.run_id = ?" if run_id is not None else ""}
            """,
            params,
        )
        for row in cur.fetchall():
            child_state = row["child_state"]
            if child_state == "INDETERMINATE":
                # keep waiting for reconciliation
                continue
            # child reached a terminal state we can act on
            cur2 = conn.execute(
                "UPDATE agent_task_waits SET state='RESOLVED', resolved_at=? "
                "WHERE wait_id=? AND state='WAITING'",
                (ts, row["wait_id"]),
            )
            if cur2.rowcount != 1:
                continue
            new_waiter_state = self._waiter_resume_state(
                row["child_state"], row["failure_policy"]
            )
            if new_waiter_state is not None:
                conn.execute(
                    "UPDATE agent_tasks SET state=?, version=version+1, "
                    "updated_at=? WHERE task_id=? AND state IN "
                    "('WAITING_CHILD','WAITING_MESSAGE','READY','PLANNED')",
                    (new_waiter_state, ts, row["waiter_task_id"]),
                )
            report["resolved_wait_ids"].append(row["wait_id"])

    @staticmethod
    def _waiter_resume_state(
        child_state: str, failure_policy: str
    ) -> str | None:
        """Map (child terminal state, failure_policy) to waiter next state."""
        if child_state == "SUCCEEDED":
            return "READY"
        if child_state == "FAILED":
            if failure_policy == "FAIL_WAITER":
                return "FAILED"
            return "READY"  # RESUME_WITH_FAILURE
        if child_state == "CANCELLED":
            return "READY"
        return None

    def _promote_planned(
        self,
        conn,
        run_id: str | None,
        ts: str,
        report: dict,
    ) -> None:
        """Promote PLANNED tasks whose dependencies are satisfied.

        ``ON_SUCCESS``: all deps SUCCEEDED
        ``ON_TERMINAL``: all deps in (SUCCEEDED, FAILED, CANCELLED)
        """
        where_extra = "AND t.run_id = ?" if run_id is not None else ""
        params: tuple = (run_id,) if run_id is not None else ()
        cur = conn.execute(
            f"""
            SELECT t.task_id, t.run_id
            FROM agent_tasks t
            WHERE t.state = 'PLANNED'
              {where_extra}
            """,
            params,
        )
        candidates = [dict(r) for r in cur.fetchall()]
        for cand in candidates:
            deps = conn.execute(
                "SELECT depends_on_task_id, condition "
                "FROM agent_task_dependencies WHERE task_id=? AND run_id=?",
                (cand["task_id"], cand["run_id"]),
            ).fetchall()
            if not deps:
                self._promote_one(conn, cand["task_id"], ts, report)
                continue
            all_satisfied = True
            for d in deps:
                dep_state = conn.execute(
                    "SELECT state FROM agent_tasks WHERE task_id=?",
                    (d["depends_on_task_id"],),
                ).fetchone()
                state = dep_state[0] if dep_state else None
                if d["condition"] == "ON_SUCCESS":
                    if state != "SUCCEEDED":
                        all_satisfied = False
                        break
                else:  # ON_TERMINAL
                    if state not in ("SUCCEEDED", "FAILED", "CANCELLED"):
                        all_satisfied = False
                        break
            if all_satisfied:
                self._promote_one(conn, cand["task_id"], ts, report)

    @staticmethod
    def _promote_one(conn, task_id: str, ts: str, report: dict) -> None:
        cur = conn.execute(
            "UPDATE agent_tasks SET state='READY', version=version+1, "
            "updated_at=? WHERE task_id=? AND state='PLANNED'",
            (ts, task_id),
        )
        if cur.rowcount == 1:
            report["promoted_task_ids"].append(task_id)

    def _aggregate_runs(
        self,
        conn,
        run_id: str | None,
        ts: str,
        report: dict,
    ) -> None:
        """Aggregate run state per spec §20.3.

        Order:
        1. INDETERMINATE → NEEDS_RECONCILIATION
        2. cancelled (no RUNNING) → CANCELLED
        3. WAITING_APPROVAL → WAITING_APPROVAL
        4. WAITING_MESSAGE → WAITING_USER
        5. required failure → FAILED
        6. WAITING_CHILD or other non-terminal → RUNNING
        7. AGENT_ANALYSIS + all required SUCCEEDED + optional failure → PARTIAL_SUCCESS
        8. AGENT_ANALYSIS + all required SUCCEEDED → SUCCEEDED
        9. SYSTEM_COMMAND + only task SUCCEEDED → SUCCEEDED
        10. SYSTEM_COMMAND task FAILED → FAILED
        11. SYSTEM_COMMAND task CANCELLED → CANCELLED
        """
        where_extra = "AND run_id = ?" if run_id is not None else ""
        params: tuple = (run_id,) if run_id is not None else ()
        cur = conn.execute(
            f"""
            SELECT run_id, run_kind, state FROM agent_runs
            WHERE state NOT IN {self._sql_in(_RUN_TERMINAL_STATES)}
              {where_extra}
            """,
            params,
        )
        active = [dict(r) for r in cur.fetchall()]
        for ar in active:
            new_state = self._compute_run_state(conn, ar)
            if new_state is None or new_state == ar["state"]:
                continue
            cur = conn.execute(
                "UPDATE agent_runs SET state=?, version=version+1, "
                "updated_at=? WHERE run_id=? AND state=?",
                (new_state, ts, ar["run_id"], ar["state"]),
            )
            if cur.rowcount == 1:
                report["aggregated_run_ids"].append(ar["run_id"])

    @staticmethod
    def _sql_in(values: tuple[str, ...]) -> str:
        return "(" + ",".join("'" + v.replace("'", "''") + "'" for v in values) + ")"

    @staticmethod
    def _compute_run_state(conn, run: dict) -> str | None:
        cur = conn.execute(
            "SELECT state, required FROM agent_tasks WHERE run_id=?",
            (run["run_id"],),
        )
        tasks = cur.fetchall()
        if not tasks:
            return None
        states = [t[0] for t in tasks]
        required_states = [t[0] for t in tasks if t[1]]
        run_kind = run["run_kind"]

        # 1. INDETERMINATE
        if any(s == "INDETERMINATE" for s in states):
            return "NEEDS_RECONCILIATION"
        # 2. cancelled: any CANCELLED + no RUNNING/WAITING_*
        if any(s == "CANCELLED" for s in states) and not any(
            s in ("RUNNING", "WAITING_CHILD", "WAITING_MESSAGE",
                  "WAITING_APPROVAL") for s in states
        ):
            return "CANCELLED"
        # 3. WAITING_APPROVAL
        if any(s == "WAITING_APPROVAL" for s in states):
            return "WAITING_APPROVAL"
        # 4. WAITING_MESSAGE
        if any(s == "WAITING_MESSAGE" for s in states):
            return "WAITING_USER"
        # 5. required failure (non-recoverable)
        if any(s == "FAILED" for s in required_states):
            return "FAILED"
        # 6. WAITING_CHILD or non-terminal → RUNNING
        if any(
            s in ("WAITING_CHILD", "RUNNING", "READY", "PLANNED")
            for s in states
        ):
            return "RUNNING"

        # 7-11: terminal combinations
        if run_kind == "AGENT_ANALYSIS":
            all_required_succeeded = all(
                s == "SUCCEEDED" for s in required_states
            )
            if all_required_succeeded:
                optional_failed = [
                    s for s in states
                    if s not in ("SUCCEEDED",) and not _is_required(tasks, s)
                ]
                # 简化:只看 optional 是否 FAILED / CANCELLED
                optional_failure_states = [
                    s for (s, req) in zip(states, [t[1] for t in tasks])
                    if not req and s in ("FAILED", "CANCELLED")
                ]
                if optional_failure_states:
                    return "PARTIAL_SUCCESS"
                return "SUCCEEDED"
            return "FAILED"

        if run_kind == "SYSTEM_COMMAND":
            # 找到 kind=SYSTEM_COMMAND 的 task
            cur = conn.execute(
                "SELECT state FROM agent_tasks "
                "WHERE run_id=? AND kind='SYSTEM_COMMAND'",
                (run["run_id"],),
            )
            sys_states = [r[0] for r in cur.fetchall()]
            if sys_states:
                if any(s == "FAILED" for s in sys_states):
                    return "FAILED"
                if any(s == "CANCELLED" for s in sys_states):
                    return "CANCELLED"
                if all(s == "SUCCEEDED" for s in sys_states):
                    return "SUCCEEDED"
            return "FAILED"

        # 未识别的 run_kind + 全 SUCCEEDED → SUCCEEDED
        if all(s == "SUCCEEDED" for s in states):
            return "SUCCEEDED"
        return "FAILED"


__all__ = ["TaskScheduler"]


def _is_required(tasks: list[tuple], state: str) -> bool:
    """Helper: check whether tasks with given state are required."""
    return any(req == 1 for (s, req) in tasks if s == state)
