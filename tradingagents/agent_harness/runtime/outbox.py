"""Task 5 — OutboxWorker.

Spec §19.2:
- 业务事务写 AgentMessage、任务新状态和一个或多个 PENDING outbox row
- OutboxWorker 用 compare-and-set 将到期 PENDING row claim 为 CLAIMED
- 投递成功写 DELIVERED;失败写回 PENDING,attempts += 1,退避为 min(2**attempts, 60) 秒
- 10 次失败后写 DEAD,并使内部 scheduler destination 对应的 run FAILED
- 外部 audit/event projection 的 DEAD 不改变已完成 run,但进入 health warning
- CLAIMED 超过 60 秒视为 lease 过期,恢复为 PENDING
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .persistence.events import (
    MAX_DELIVERY_ATTEMPTS,
    RETRY_BACKOFF_CAP_SECONDS,
    OutboxRepository,
)

LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry_backoff_seconds(attempts: int) -> int:
    """min(2 ** attempts, 60) 秒(spec §19.2)."""
    return min(2 ** attempts, RETRY_BACKOFF_CAP_SECONDS)


class OutboxWorker:
    """Claims and delivers due outbox rows."""

    def __init__(self, store, *, handlers: dict[str, Callable[[dict[str, Any]], bool]]):
        self._store = store
        self._handlers = handlers
        self._outbox_repo = OutboxRepository(store)

    def run_once(self, *, limit: int = 100, now: str | None = None,
                 worker_id: str = "outbox-worker") -> dict[str, int]:
        """单次扫描 + 投递循环。

        Spec §19.2 item 5:CLAIMED 超过 lease 秒视为 lease 过期,
        恢复为 PENDING 后再被 claim_due 重新认领。
        """
        now = now or _now_iso()
        # 先把过期 CLAIMED 回收为 PENDING
        self._outbox_repo.reset_expired_claims(now=now)
        claimed = self._outbox_repo.claim_due(worker_id=worker_id, limit=limit, now=now)
        delivered = 0
        failed = 0
        dead = 0
        for row in claimed:
            handler = self._handlers.get(row["destination"])
            if handler is None:
                # 未知 destination:直接 DEAD,记 error
                self._outbox_repo.mark_dead(
                    outbox_id=row["outbox_id"],
                    error=f"no handler for destination={row['destination']}",
                    now=now,
                )
                dead += 1
                continue
            try:
                payload = self._decode_payload(row["payload_json"])
                handler(payload)
            except Exception as e:
                attempts = int(row["attempts"]) + 1
                if attempts >= MAX_DELIVERY_ATTEMPTS:
                    self._outbox_repo.mark_dead(
                        outbox_id=row["outbox_id"],
                        error=f"{type(e).__name__}: {e}",
                        now=now,
                    )
                    self._on_dead(row, error=str(e), now=now)
                    dead += 1
                else:
                    backoff = _retry_backoff_seconds(attempts)
                    self._outbox_repo.mark_failed_for_retry(
                        outbox_id=row["outbox_id"],
                        error=f"{type(e).__name__}: {e}",
                        now=now,
                        backoff_seconds=backoff,
                    )
                    failed += 1
                continue
            self._outbox_repo.mark_delivered(outbox_id=row["outbox_id"], now=now)
            delivered += 1
        return {
            "claimed": len(claimed),
            "delivered": delivered,
            "failed": failed,
            "dead": dead,
        }

    def _on_dead(self, row: dict[str, Any], *, error: str, now: str) -> None:
        """DEAD 后副作用:
        - scheduler destination → run FAILED (terminal_reason = OUTBOX_SCHEDULER_DEAD)
        - audit/event_log/sse → 仅 health warning,run 状态不变
        """
        destination = row["destination"]
        if destination == "scheduler":
            self._fail_run_for_scheduler_dead(
                run_id=row["run_id"], error=error, now=now,
            )
        else:
            LOGGER.warning(
                "outbox_dead destination=%s outbox_id=%s run_id=%s error=%s",
                destination, row["outbox_id"], row["run_id"], error,
            )

    def _fail_run_for_scheduler_dead(self, *, run_id: str, error: str, now: str) -> None:
        from .persistence.runs import RunRepository
        rr = RunRepository(self._store)
        try:
            rr.mark_terminal(
                run_id=run_id,
                expected_state="PLANNING",  # best-effort,无论当前什么状态都尝试
                final_seq=0,
                final_result_json={"outbox_error": error},
                terminal_reason="OUTBOX_SCHEDULER_DEAD",
                now=now,
            )
        except RuntimeError:
            # 当前状态不是 PLANNING,改用更宽松的失败路径
            current = rr.get_run(run_id)
            if current and current["state"] not in (
                "SUCCEEDED", "FAILED", "CANCELLED", "PARTIAL_SUCCESS", "LEGACY_INTERRUPTED",
            ):
                rr.mark_terminal(
                    run_id=run_id,
                    expected_state=current["state"],
                    final_seq=0,
                    final_result_json={"outbox_error": error},
                    terminal_reason="OUTBOX_SCHEDULER_DEAD",
                    now=now,
                )

    @staticmethod
    def _decode_payload(payload_json: Any) -> dict[str, Any]:
        import json
        if isinstance(payload_json, (bytes, bytearray)):
            payload_json = payload_json.decode("utf-8")
        if isinstance(payload_json, str):
            try:
                return json.loads(payload_json)
            except json.JSONDecodeError:
                return {"_raw": payload_json}
        if isinstance(payload_json, dict):
            return payload_json
        return {"_raw": str(payload_json)}


__all__ = ["OutboxWorker"]
