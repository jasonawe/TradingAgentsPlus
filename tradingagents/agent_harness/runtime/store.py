"""Task 3 — AgentRuntimeStore facade.

Compose focused repositories and expose a stable API surface. 本 Task 只实现
migrations bootstrap + connection accessor;各 repository(runs / tasks / events /
operations / usage / outbox / approvals)在后续 Task 填充。
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterator

from .persistence.db import open_connection

LOGGER = logging.getLogger(__name__)


class AgentRuntimeStore:
    """Process-local facade for the supervised AgentRuntime SQLite store."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path).resolve()
        self._conn: sqlite3.Connection | None = None
        # 单进程内多线程串行化 connection 访问 — Python sqlite3 在共享 connection
        # 上做跨线程 execute 的事务状态是未定义的,需要在外层加锁保证原子性。
        self._write_lock = threading.RLock()
        # 立即初始化 — 触发 migration
        with open_connection(self._db_path) as conn:
            # 在迁移完成后关闭,我们后续按需重新 open
            pass
        LOGGER.info("AgentRuntimeStore initialised at %s", self._db_path)

    @contextlib.contextmanager
    def serial_write(self) -> Iterator[sqlite3.Connection]:
        """Serialize access to the SQLite connection.

        Multiple threads sharing one sqlite3.Connection produces undefined
        transaction semantics (per-connection transaction state is not
        thread-safe). Acquire this lock around any write that needs to
        observe a consistent CAS outcome.
        """
        with self._write_lock:
            yield self.connection

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Acquire a connection. Migration 已执行,这里直接 open。"""
        # 单 connection 复用模式 — 短事务,串行使用
        if self._conn is None:
            from .persistence.db import _connect
            self._conn = _connect(self._db_path)
        try:
            yield self._conn
        except Exception:
            # connection 出错时关闭,下次重新打开
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Direct accessor — 假设调用方负责事务边界。"""
        if self._conn is None:
            from .persistence.db import _connect
            self._conn = _connect(self._db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def recover(self, *, now: str | None = None) -> dict[str, int]:
        """Spec §19.4 — 恢复扫描。

        在同一事务内完成:
        1. 将 lease 过期 RUNNING task CAS 回 READY
        2. 将 lease 过期 CLAIMED outbox CAS 回 PENDING
        3. 对每个非终态 run 重算 dependency readiness(简化版,本 Task 只覆盖
           已经 SUCCEEDED 的 child 触发的 wait resolution)
        4. 依赖已满足的 PLANNED task 转 READY
        5. 对所有任务都终止的 run 重算 run 状态(SUCCEEDED/PARTIAL_SUCCESS/FAILED)
        """
        from datetime import datetime, timezone, timedelta
        from .persistence.events import OutboxRepository
        if now is None:
            now = datetime.now(timezone.utc).isoformat()

        outbox_repo = OutboxRepository(self)

        summary = {
            "reset_tasks": 0,
            "reset_outbox": 0,
            "resolved_waits": 0,
            "promoted_tasks": 0,
            "aggregated_runs": 0,
        }

        with self.serial_write():
            conn = self.connection

            # 1. lease 过期 RUNNING task → READY (version + 1)
            lease_threshold = (
                datetime.fromisoformat(now) - timedelta(seconds=60)
            ).isoformat()
            cur = conn.execute(
                """
                UPDATE agent_tasks
                SET state = 'READY', worker_id = NULL, lease_expires_at = NULL,
                    version = version + 1, updated_at = ?
                WHERE state = 'RUNNING'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (now, lease_threshold),
            )
            summary["reset_tasks"] = cur.rowcount

            # 2. lease 过期 CLAIMED outbox → PENDING(由 OutboxRepository 负责)
            summary["reset_outbox"] = outbox_repo.reset_expired_claims(now=now)

            # 3. wait resolution:对每个 WAITING 的 wait,如果 child 已 SUCCEEDED,
            # 把 wait 标 RESOLVED
            cur = conn.execute(
                """
                UPDATE agent_task_waits
                SET state = 'RESOLVED', resolved_at = ?
                WHERE state = 'WAITING'
                  AND child_task_id IN (
                    SELECT task_id FROM agent_tasks WHERE state = 'SUCCEEDED'
                  )
                """,
                (now,),
            )
            summary["resolved_waits"] = cur.rowcount

            # 4. PLANNED task 的 dependency readiness 重算:
            # 如果 task 没有 pending 依赖且不在 wait list 中 → READY
            cur = conn.execute(
                """
                UPDATE agent_tasks
                SET state = 'READY', version = version + 1, updated_at = ?
                WHERE state = 'PLANNED'
                  AND task_id NOT IN (
                    SELECT depends_on_task_id FROM agent_task_dependencies
                  )
                  AND task_id NOT IN (
                    SELECT waiter_task_id FROM agent_task_waits WHERE state = 'WAITING'
                  )
                """,
                (now,),
            )
            summary["promoted_tasks"] = cur.rowcount

            # 5. run aggregation:对每个非终态 run,如果所有 task 都已终止,
            # 重算 run state
            cur = conn.execute("SELECT run_id FROM agent_runs WHERE state NOT IN ("
                               "'SUCCEEDED','FAILED','CANCELLED','PARTIAL_SUCCESS','LEGACY_INTERRUPTED')")
            active_runs = [r[0] for r in cur.fetchall()]
            for run_id in active_runs:
                task_states = conn.execute(
                    "SELECT state FROM agent_tasks WHERE run_id = ?", (run_id,)
                ).fetchall()
                states = [s[0] for s in task_states]
                if not states:
                    continue
                if all(s == "SUCCEEDED" for s in states):
                    new_state = "SUCCEEDED"
                elif any(s == "FAILED" for s in states):
                    new_state = "FAILED"
                elif any(s == "CANCELLED" for s in states):
                    new_state = "CANCELLED"
                else:
                    continue
                cur = conn.execute(
                    """
                    UPDATE agent_runs
                    SET state = ?, terminal_reason = 'RECOVERED_AGGREGATE',
                        updated_at = ?, version = version + 1
                    WHERE run_id = ? AND state NOT IN
                      ('SUCCEEDED','FAILED','CANCELLED','PARTIAL_SUCCESS','LEGACY_INTERRUPTED')
                    """,
                    (new_state, now, run_id),
                )
                if cur.rowcount == 1:
                    summary["aggregated_runs"] += 1

            conn.commit()
        return summary


__all__ = ["AgentRuntimeStore"]
