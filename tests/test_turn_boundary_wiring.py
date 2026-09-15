"""Q1 / P2-4 — Orchestrator 集成 turn/step/turn-ended。

覆盖:
  - stream_chat yield 顺序: turn/started → step/started(5 个) → ... → turn/ended
  - turn/ended payload 含 turn_id / end_reason / duration_s / step_count
  - 正常完成 end_reason=USER_DONE
  - 异常完成 end_reason=ERROR
  - Orchestrator.turn_history 累积 TurnRecord
  - 多个连续 turn 产生递增 turn_id
  - step_id 单调递增 + 全部有 status
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def harness(tmp_path):
    from tradingagents.agent_harness.harness import Harness, HarnessConfig
    return Harness(HarnessConfig(data_dir=tmp_path))


@pytest.fixture
def stubbed_nodes():
    """Stub out the 5 orchestrator nodes so stream_chat doesn't hit the LLM."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator

    async def fake_plan(self, state, context):
        return [{"action": "get_quote", "args": {"symbol": "X"}}]

    async def fake_execute(self, state, context):
        return [{"name": "get_quote", "result": {"price": 1.0}}]

    def fake_observe(self, state):
        return {"items": []}

    async def fake_verify(self, state):
        from tradingagents.agent_harness.core.verification import VerificationResult
        return VerificationResult(ok=True, level=0, details="ok")

    async def fake_synthesize(self, state):
        return "synthesized answer"

    originals = {
        "_plan": Orchestrator._plan,
        "_execute": Orchestrator._execute,
        "_observe": Orchestrator._observe,
        "_verify": Orchestrator._verify,
        "_synthesize": Orchestrator._synthesize,
    }
    Orchestrator._plan = fake_plan
    Orchestrator._execute = fake_execute
    Orchestrator._observe = fake_observe
    Orchestrator._verify = fake_verify
    Orchestrator._synthesize = fake_synthesize
    yield Orchestrator
    for name, fn in originals.items():
        setattr(Orchestrator, name, fn)


def test_normal_turn_emits_full_lifecycle(harness, stubbed_nodes):
    events: list[tuple[str, dict]] = []

    async def _drain():
        async for ev_payload in harness.orchestrator.stream_chat("sessA", "分析 AAPL"):
            ev, payload = ev_payload
            events.append((ev, payload))

    asyncio.run(_drain())

    types = [e[0] for e in events]
    # turn/started 是第一个,turn/ended 在 events 中(可能不是最后一个,因为
    # 后续 usage_summary / error 等由 stream_chat 包装层 emit)
    assert types[0] == "turn/started"
    assert "turn/ended" in types

    step_started = [t for t in types if t == "step/started"]
    step_ended = [t for t in types if t == "step/ended"]
    assert len(step_started) == len(step_ended)
    assert len(step_started) == 5  # planning / executing / observing / verifying / synthesizing

    turn_ended_payload = next(p for ev, p in events if ev == "turn/ended")
    assert turn_ended_payload["end_reason"] == "user_done"
    assert turn_ended_payload["step_count"] == 5
    assert "duration_s" in turn_ended_payload
    assert turn_ended_payload["session_id"] == "sessA"


def test_turn_record_appended_to_history(harness, stubbed_nodes):
    assert len(harness.orchestrator._turn_history) == 0

    async def _drain():
        async for ev in harness.orchestrator.stream_chat("sessB", "分析 NVDA"):
            pass

    asyncio.run(_drain())
    history = harness.orchestrator._turn_history
    assert len(history) == 1
    rec = history[0]
    assert rec.session_id == "sessB"
    assert rec.end_reason.value == "user_done"
    assert rec.step_count == 5


def test_multiple_turns_get_monotonic_ids(harness, stubbed_nodes):
    async def _run_one(sid):
        async for ev in harness.orchestrator.stream_chat(sid, "分析 GOOG"):
            pass

    for i in range(3):
        asyncio.run(_run_one(f"sessC{i}"))

    history = harness.orchestrator._turn_history
    assert len(history) == 3
    ids = [r.turn_id for r in history]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3


def test_error_turn_records_error_reason(harness, stubbed_nodes):
    """turn 抛错时,turn/ended 用 ERROR reason,history 仍有记录。"""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator

    async def boom(self, state, context):
        raise RuntimeError("simulated failure")

    Orchestrator._plan = boom

    async def _drain():
        try:
            async for ev in harness.orchestrator.stream_chat("sessD", "分析 TSLA"):
                pass
        except RuntimeError:
            pass

    asyncio.run(_drain())

    history = harness.orchestrator._turn_history
    assert len(history) == 1
    rec = history[0]
    assert rec.end_reason.value == "error"
    # step at least marked as error
    assert any(s.status == "error" for s in rec.steps)


def test_history_capped_at_limit(harness, stubbed_nodes):
    harness.orchestrator._turn_history_limit = 2

    async def _run_one(sid):
        async for ev in harness.orchestrator.stream_chat(sid, "x"):
            pass

    for i in range(5):
        asyncio.run(_run_one(f"sessE{i}"))

    assert len(harness.orchestrator._turn_history) == 2
    assert harness.orchestrator._turn_history[-1].session_id == "sessE4"
    assert harness.orchestrator._turn_history[-2].session_id == "sessE3"


def test_step_records_have_increasing_step_id(harness, stubbed_nodes):
    async def _drain():
        async for ev in harness.orchestrator.stream_chat("sessF", "x"):
            pass

    asyncio.run(_drain())
    rec = harness.orchestrator._turn_history[0]
    step_ids = [s.step_id for s in rec.steps]
    assert step_ids == [1, 2, 3, 4, 5]
    assert all(s.status == "ok" for s in rec.steps)
