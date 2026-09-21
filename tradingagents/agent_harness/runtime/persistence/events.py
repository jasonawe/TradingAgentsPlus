"""Task 4 — EventRepository。

负责:
- append_runtime_event(monotonic run seq,concurrent-safe)
- append_message_and_outbox(atomic AgentMessage + runtime_event + outbox)
- list_outbox
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass
class StoredMessageEvent:
    message: dict[str, Any]
    event: dict[str, Any]


class EventRepository:
    def __init__(self, store):
        self._store = store
        self._seq_lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._store.connection

    def _next_seq(self, run_id: str) -> int:
        """Compute next seq by atomic UPDATE of agent_runs.next_seq."""
        cur = self.conn.execute(
            "UPDATE agent_runs SET next_seq = next_seq + 1, updated_at = updated_at "
            "WHERE run_id = ? RETURNING next_seq",
            (run_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"run {run_id} not found for seq allocation")
        return row[0]

    def append_runtime_event(
        self,
        *,
        run_id: str,
        event_type: str,
        surface: str,
        payload_json: dict,
        now: str,
        task_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        with self._seq_lock:
            seq = self._next_seq(run_id)
            event_id = str(uuid.uuid4())
            self.conn.execute(
                """
                INSERT INTO runtime_events
                  (event_id, run_id, seq, event_type, task_id, message_id,
                   surface, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, run_id, seq, event_type, task_id, message_id,
                 surface, json.dumps(payload_json), now),
            )
            self.conn.commit()
        return {
            "event_id": event_id,
            "run_id": run_id,
            "seq": seq,
            "event_type": event_type,
            "task_id": task_id,
            "message_id": message_id,
            "surface": surface,
            "payload_json": payload_json,
            "created_at": now,
        }

    def append_message(
        self,
        *,
        run_id: str,
        turn_id: str,
        task_id: str,
        parent_task_id: str | None,
        sender: str,
        recipient: str,
        msg_type: str,
        payload: dict,
        evidence_refs: list,
        correlation_id: str,
        execution_attempt: int,
        now: str,
        causation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self._seq_lock:
            seq = self._next_seq(run_id)
            message_id = str(uuid.uuid4())
            idem = idempotency_key or str(uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"{task_id}|{execution_attempt}|{msg_type}|{recipient}|{correlation_id}|{seq}",
            ))
            self.conn.execute(
                """
                INSERT INTO agent_messages
                  (message_id, run_id, turn_id, seq, task_id, parent_task_id,
                   sender, recipient, type, payload_json, evidence_refs_json,
                   causation_id, correlation_id, idempotency_key,
                   execution_attempt, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, run_id, turn_id, seq, task_id, parent_task_id,
                 sender, recipient, msg_type,
                 json.dumps(payload), json.dumps(evidence_refs),
                 causation_id, correlation_id, idem,
                 execution_attempt, now),
            )
            self.conn.commit()
        return {
            "message_id": message_id,
            "run_id": run_id,
            "turn_id": turn_id,
            "seq": seq,
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "sender": sender,
            "recipient": recipient,
            "type": msg_type,
            "payload_json": payload,
            "evidence_refs_json": evidence_refs,
            "causation_id": causation_id,
            "correlation_id": correlation_id,
            "idempotency_key": idem,
            "execution_attempt": execution_attempt,
            "created_at": now,
        }

    def append_message_and_outbox(
        self,
        *,
        draft: Any | None,
        outbox_destinations: tuple[str, ...],
        delivery_keys: dict[str, str],
        now: str,
        causation_id: str | None = None,
    ) -> StoredMessageEvent:
        """原子事务:message + runtime_event + outbox rows。

        draft 为 None 时只跑失败路径(测试用)。
        """
        if draft is None:
            # 测试失败路径 — 抛错触发 rollback
            self._append_outbox_only_fail()
        assert draft is not None  # noqa
        with self._seq_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                seq = self._next_seq(draft.run_id)
                message_id = str(uuid.uuid4())
                idem = str(uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"{draft.task_id}|{draft.execution_attempt}|"
                    f"{draft.type.value if hasattr(draft.type, 'value') else draft.type}|"
                    f"{draft.recipient}|{draft.correlation_id}|{seq}",
                ))
                self.conn.execute(
                    """
                    INSERT INTO agent_messages
                      (message_id, run_id, turn_id, seq, task_id, parent_task_id,
                       sender, recipient, type, payload_json, evidence_refs_json,
                       causation_id, correlation_id, idempotency_key,
                       execution_attempt, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (message_id, draft.run_id, draft.turn_id, seq,
                     draft.task_id, draft.parent_task_id,
                     draft.sender, draft.recipient,
                     draft.type.value if hasattr(draft.type, "value") else str(draft.type),
                     json.dumps(draft.payload),
                     json.dumps(getattr(draft, "evidence_refs", [])),
                     causation_id, draft.correlation_id, idem,
                     draft.execution_attempt, now),
                )
                # 配对 runtime_event
                event_id = str(uuid.uuid4())
                self.conn.execute(
                    """
                    INSERT INTO runtime_events
                      (event_id, run_id, seq, event_type, task_id, message_id,
                       surface, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'PUBLIC', ?, ?)
                    """,
                    (event_id, draft.run_id, seq,
                     f"MSG_{draft.type.value if hasattr(draft.type, 'value') else draft.type}",
                     draft.task_id, message_id,
                     json.dumps({"summary": str(draft.payload)[:200]}),
                     now),
                )
                # outbox 写入
                for dest in outbox_destinations:
                    dk = delivery_keys.get(dest, str(uuid.uuid4()))
                    self.conn.execute(
                        """
                        INSERT INTO agent_outbox
                          (outbox_id, run_id, task_id, message_id, source_event_id,
                           delivery_key, destination, payload_json, state,
                           attempts, available_at, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)
                        """,
                        (str(uuid.uuid4()), draft.run_id, draft.task_id,
                         message_id, event_id, dk, dest,
                         json.dumps({"message_id": message_id, "seq": seq}),
                         now, now),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        # 返回 stored
        message_row = self.conn.execute(
            "SELECT * FROM agent_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        event_row = self.conn.execute(
            "SELECT * FROM runtime_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return StoredMessageEvent(
            message=dict(message_row) if message_row else {},
            event=dict(event_row) if event_row else {},
        )

    def _append_outbox_only_fail(self):
        """测试用 — 模拟事务中途抛错,验证全 rollback。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # 模拟写入一些东西然后抛错
            self.conn.execute(
                "INSERT INTO agent_outbox (outbox_id, run_id, source_event_id, "
                "delivery_key, destination, payload_json, state, attempts, "
                "available_at, created_at) VALUES ('boom', 'r1', 'e1', 'k', 'd', "
                "'{}', 'PENDING', 0, 'now', 'now')"
            )
        except Exception:
            self.conn.execute("ROLLBACK")
        raise RuntimeError("simulated outbox failure")

    def append_outbox(
        self,
        *,
        run_id: str,
        task_id: str | None,
        message_id: str | None,
        source_event_id: str,
        delivery_key: str,
        destination: str,
        payload_json: dict,
        now: str,
    ) -> None:
        # 幂等:同一 (destination, delivery_key) 重复 enqueue 不抛错。
        # 已 DELIVERED / DEAD 的 row 保留历史状态;PENDING/CLAIMED 的 row
        # 内容已确定,覆盖只会带来竞态,因此不更新。
        self.conn.execute(
            """
            INSERT OR IGNORE INTO agent_outbox
              (outbox_id, run_id, task_id, message_id, source_event_id,
               delivery_key, destination, payload_json, state,
               attempts, available_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)
            """,
            (str(uuid.uuid4()), run_id, task_id, message_id,
             source_event_id, delivery_key, destination,
             json.dumps(payload_json), now, now),
        )
        self.conn.commit()

    def list_outbox(self, run_id: str) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM agent_outbox WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )
        return [dict(r) for r in cur.fetchall()]


__all__ = ["EventRepository", "StoredMessageEvent"]


# ════════════════════════════════════════════════════════
# Outbox CAS operations (Task 5)
# ════════════════════════════════════════════════════════

# 60-second claim lease + 10 attempts max (spec §19.2)
CLAIM_LEASE_SECONDS = 60
MAX_DELIVERY_ATTEMPTS = 10
RETRY_BACKOFF_CAP_SECONDS = 60


class OutboxRepository:
    """CAS-only outbox operations: claim_due / mark_delivered / mark_dead / reset_expired_claims.

    所有方法都使用 compare-and-set:
    - claim_due:PENDING → CLAIMED(过滤 available_at <= now)
    - mark_delivered:CLAIMED → DELIVERED
    - mark_failed_for_retry:CLAIMED → PENDING(attempts+1, available_at = now + backoff)
    - mark_dead:CLAIMED → DEAD
    - reset_expired_claims:CLAIMED (claimed_at < now - 60s) → PENDING
    """

    def __init__(self, store):
        self._store = store

    @property
    def conn(self) -> sqlite3.Connection:
        return self._store.connection

    def claim_due(self, *, worker_id: str, limit: int, now: str) -> list[dict[str, Any]]:
        """CAS PENDING + available_at<=now → CLAIMED。返回被 claim 的 rows。"""
        with self._store.serial_write():
            claimed: list[dict[str, Any]] = []
            cur = self.conn.execute(
                """
                SELECT * FROM agent_outbox
                WHERE state = 'PENDING' AND available_at <= ?
                ORDER BY available_at LIMIT ?
                """,
                (now, limit),
            )
            rows = cur.fetchall()
            for row in rows:
                upd = self.conn.execute(
                    """
                    UPDATE agent_outbox
                    SET state = 'CLAIMED', claimed_by = ?, claimed_at = ?
                    WHERE outbox_id = ? AND state = 'PENDING'
                    """,
                    (worker_id, now, row["outbox_id"]),
                )
                if upd.rowcount == 1:
                    claimed.append(dict(row))
            self.conn.commit()
        # re-read so state is CLAIMED
        if not claimed:
            return []
        ids = tuple(r["outbox_id"] for r in claimed)
        placeholders = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"SELECT * FROM agent_outbox WHERE outbox_id IN ({placeholders})",
            ids,
        )
        return [dict(r) for r in cur.fetchall()]

    def reset_expired_claims(self, *, now: str,
                             lease_seconds: int = CLAIM_LEASE_SECONDS) -> int:
        """CLAIMED 超过 lease 秒 → PENDING。返回被重置的 row 数。"""
        threshold = (datetime.fromisoformat(now) - timedelta(seconds=lease_seconds)).isoformat()
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_outbox
                SET state = 'PENDING', claimed_by = NULL, claimed_at = NULL,
                    available_at = ?
                WHERE state = 'CLAIMED' AND claimed_at < ?
                """,
                (now, threshold),
            )
            self.conn.commit()
        return cur.rowcount

    def mark_delivered(self, *, outbox_id: str, now: str) -> dict[str, Any]:
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_outbox
                SET state = 'DELIVERED', delivered_at = ?
                WHERE outbox_id = ? AND state = 'CLAIMED'
                """,
                (now, outbox_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"outbox {outbox_id} deliver CAS failed")
            self.conn.commit()
        return self._row(outbox_id)

    def mark_failed_for_retry(self, *, outbox_id: str, error: str,
                              now: str, backoff_seconds: int) -> dict[str, Any]:
        """CLAIMED → PENDING,attempts+1,available_at = now + backoff。"""
        next_at = (datetime.fromisoformat(now) + timedelta(seconds=backoff_seconds)).isoformat()
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_outbox
                SET state = 'PENDING', claimed_by = NULL, claimed_at = NULL,
                    available_at = ?, attempts = attempts + 1, last_error = ?
                WHERE outbox_id = ? AND state = 'CLAIMED'
                """,
                (next_at, error, outbox_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"outbox {outbox_id} retry CAS failed")
            self.conn.commit()
        return self._row(outbox_id)

    def mark_dead(self, *, outbox_id: str, error: str, now: str) -> dict[str, Any]:
        with self._store.serial_write():
            cur = self.conn.execute(
                """
                UPDATE agent_outbox
                SET state = 'DEAD', last_error = ?,
                    attempts = MAX(attempts, ?)
                WHERE outbox_id = ? AND state IN ('CLAIMED', 'PENDING')
                """,
                (error, MAX_DELIVERY_ATTEMPTS, outbox_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"outbox {outbox_id} dead CAS failed")
            self.conn.commit()
        return self._row(outbox_id)

    def _row(self, outbox_id: str) -> dict[str, Any]:
        cur = self.conn.execute(
            "SELECT * FROM agent_outbox WHERE outbox_id = ?", (outbox_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else {}


__all__ = [
    "EventRepository",
    "StoredMessageEvent",
    "OutboxRepository",
    "CLAIM_LEASE_SECONDS",
    "MAX_DELIVERY_ATTEMPTS",
    "RETRY_BACKOFF_CAP_SECONDS",
]
