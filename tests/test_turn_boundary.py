"""Q1 / P2-4 — TurnBoundary / TurnRecord / TurnEndReason。

覆盖:
  - TurnEndReason enum 值
  - TurnBoundary 分配单调递增 turn_id(线程安全)
  - StepRecord 基本字段 + duration_s 计算
  - TurnRecord.to_dict 序列化
  - 完整 turn 流程:start → 2 steps → end with reason
"""
from __future__ import annotations

import threading

import pytest


def test_turn_end_reason_values():
    from tradingagents.agent_harness.core.turn_boundary import TurnEndReason
    # spot-check the documented values
    assert TurnEndReason.USER_DONE.value == "user_done"
    assert TurnEndReason.STEERED.value == "steered"
    assert TurnEndReason.INJECTED.value == "injected"
    assert TurnEndReason.ERROR.value == "error"
    assert TurnEndReason.TIMEOUT.value == "timeout"
    assert TurnEndReason.INTERRUPTED.value == "interrupted"
    assert TurnEndReason.EMPTY.value == "empty"
    assert TurnEndReason.SHORT_CIRCUIT.value == "short_circuit"


def test_turn_boundary_monotonic():
    from tradingagents.agent_harness.core.turn_boundary import TurnBoundary
    tb = TurnBoundary()
    assert tb.last_turn_id == 0
    ids = [tb.next_turn_id() for _ in range(5)]
    assert ids == [1, 2, 3, 4, 5]
    assert tb.last_turn_id == 5


def test_turn_boundary_thread_safe():
    from tradingagents.agent_harness.core.turn_boundary import TurnBoundary
    tb = TurnBoundary()
    seen = []
    lock = threading.Lock()

    def worker():
        for _ in range(100):
            tid = tb.next_turn_id()
            with lock:
                seen.append(tid)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 400 unique ids, monotonic and complete
    assert len(set(seen)) == 400
    assert min(seen) == 1
    assert max(seen) == 400


def test_step_record_duration():
    from tradingagents.agent_harness.core.turn_boundary import StepRecord
    s = StepRecord(step_id=1, name="planning", started_at=100.0)
    assert s.duration_s is None
    s.finished_at = 100.5
    assert s.duration_s == 0.5
    s.status = "ok"


def test_turn_record_to_dict():
    from tradingagents.agent_harness.core.turn_boundary import (
        TurnRecord, StepRecord, TurnEndReason,
    )
    turn = TurnRecord(
        turn_id=42, session_id="s1", user_message="hi",
        started_at=1000.0, finished_at=1001.5,
        end_reason=TurnEndReason.USER_DONE,
    )
    turn.steps.append(StepRecord(step_id=1, name="planning", started_at=1000.0, finished_at=1000.3, status="ok"))
    turn.steps.append(StepRecord(step_id=2, name="executing", started_at=1000.3, finished_at=1001.5, status="ok"))

    d = turn.to_dict()
    assert d["turn_id"] == 42
    assert d["session_id"] == "s1"
    assert d["duration_s"] == 1.5
    assert d["end_reason"] == "user_done"
    assert d["step_count"] == 2
    assert len(d["steps"]) == 2
    assert d["steps"][0]["name"] == "planning"
    assert d["steps"][1]["name"] == "executing"


def test_turn_record_duration_none_until_finished():
    from tradingagents.agent_harness.core.turn_boundary import TurnRecord
    turn = TurnRecord(turn_id=1, session_id="s", user_message="x", started_at=0.0)
    assert turn.duration_s is None
    turn.finished_at = 2.0
    assert turn.duration_s == 2.0
