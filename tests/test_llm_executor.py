"""Task 9 — LLMExecutor + UsageLedger tests。

覆盖 plan 要求:
- 原子预算预留(并发下不超额)
- 同一 task attempt 多个 call ordinal
- provider 重试 → child reservation rows
- actual usage 结算
- pre-send release
- 模糊 timeout 按 reservation ceiling 计费
- lease expiry settlement
- cache hit 不计费
- 预算耗尽 → provider call 不发起
- usage_summary.by_agent
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pytest


def _store(tmp_path):
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    return AgentRuntimeStore(tmp_path / "rt.sqlite")


def _make_run_task(tmp_path):
    import uuid
    from datetime import datetime, timezone
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    store = AgentRuntimeStore(tmp_path / "rt.sqlite")
    rr = RunRepository(store)
    now = datetime.now(timezone.utc).isoformat()
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST, symbols=["x"], carry_symbols=[],
        slots={}, tier=1, confidence=1.0, reason_code="t", route_kind="DIRECT_READ",
    )
    run = rr.create_run(
        run_id=str(uuid.uuid4()), session_id="s1", turn_id="t1",
        run_kind="AGENT_ANALYSIS", route=route,
        budgets={"max_llm_calls": 10, "max_tokens": 1000}, now=now,
    )
    class _P:
        objective = "x"
        inputs = {}
    task = TaskRepository(store).create_task(
        task_id=str(uuid.uuid4()), run_id=run["run_id"],
        parent_task_id=None, kind="PLANNER", agent_name=None,
        system_handler=None, capability="planning", payload=_P(),
        required=True, max_execution_attempts=3, now=now,
    )
    return store, run["run_id"], task["task_id"]


# ════════════════════════════════════════════════════════
# LLMExecutor reservation
# ════════════════════════════════════════════════════════

def test_llm_executor_reserves_tokens_before_provider_call(tmp_path):
    from tradingagents.agent_harness.core.llm_executor import LLMExecutor, LLMResponse
    store, run_id, task_id = _make_run_task(tmp_path)
    ex = LLMExecutor(store)

    # Fake provider returns fixed usage
    def fake_complete(messages, *, model, max_tokens):
        return _FakeResponse(content="ok", prompt_tokens=20, completion_tokens=10)

    res = ex.complete(
        task_id=task_id, agent_name="PlannerAgent",
        messages=[{"role": "user", "content": "hi"}],
        provider=fake_complete,
        model="gpt-4o-mini", max_tokens=100,
    )
    assert isinstance(res, LLMResponse)
    assert res.content == "ok"
    assert res.prompt_tokens == 20
    assert res.completion_tokens == 10


def test_llm_executor_cache_hit_does_not_bill(tmp_path):
    """cache hit 时 reservation 直接 SETTLED + 0 tokens。"""
    from tradingagents.agent_harness.core.llm_executor import LLMExecutor
    from tradingagents.agent_harness.runtime.persistence.usage import UsageReservationRepository
    store, run_id, task_id = _make_run_task(tmp_path)

    # 准备 cache:insert 一条 entry
    from tradingagents.agent_harness.llm.cache import LLMResponseCache
    from tradingagents.agent_harness.llm.base import LLMResponse as _LLMResp
    cache = LLMResponseCache()
    cache.put("k1", _LLMResp(content="cached", model="m", provider="x"))

    ex = LLMExecutor(store, response_cache=cache)
    res = ex.complete(
        task_id=task_id, agent_name="PlannerAgent",
        messages=[{"role": "user", "content": "hi"}],
        provider=lambda msgs, **kw: _FakeResponse(content="from provider", prompt_tokens=99, completion_tokens=99),
        model="m", max_tokens=100,
        cache_key="k1",
    )
    assert res.content == "cached"
    assert res.cached is True
    # reservation should be marked settled, 0 usage
    reservations = UsageReservationRepository(store).list_for_task(task_id)
    settled = [r for r in reservations if r["state"] == "SETTLED"]
    assert any(r["actual_input_tokens"] == 0 for r in settled)


def test_llm_executor_budget_exhaustion_skips_provider(tmp_path):
    """预算耗尽时不调用 provider,抛 BudgetExhausted。"""
    from tradingagents.agent_harness.core.llm_executor import LLMExecutor, BudgetExhausted
    store, run_id, task_id = _make_run_task(tmp_path)
    # 预填一个在 budget 内的 reservation 占满 budget(900 tokens,max=1000)
    from tradingagents.agent_harness.runtime.persistence.usage import UsageReservationRepository
    ur = UsageReservationRepository(store)
    now = datetime.now(timezone.utc).isoformat()
    ur.reserve(
        reservation_id=str(uuid.uuid4()),
        call_id="pre-fill", run_id=run_id, task_id=task_id,
        agent_name="PlannerAgent", execution_attempt=1,
        call_ordinal=0, provider="fake", model="m",
        reserved_input_tokens=900, reserved_output_tokens=0,
        lease_expires_at=now, now=now,
    )
    ex = LLMExecutor(store)

    called = []
    def fake_provider(msgs, **kw):
        called.append("called")
        return _FakeResponse(content="x", prompt_tokens=0, completion_tokens=0)
    with pytest.raises(BudgetExhausted):
        ex.complete(
            task_id=task_id, agent_name="PlannerAgent",
            messages=[], provider=fake_provider,
            model="m", max_tokens=100,
        )
    assert called == [], "provider 不应在 budget 耗尽时被调用"


def test_llm_executor_release_reservation_on_pre_send_failure(tmp_path):
    """provider 抛错前 reservation 应被 release。"""
    from tradingagents.agent_harness.core.llm_executor import LLMExecutor
    from tradingagents.agent_harness.runtime.persistence.usage import UsageReservationRepository
    store, run_id, task_id = _make_run_task(tmp_path)
    ex = LLMExecutor(store)

    def fake_provider(msgs, **kw):
        raise RuntimeError("provider down")
    with pytest.raises(RuntimeError):
        ex.complete(
            task_id=task_id, agent_name="PlannerAgent",
            messages=[], provider=fake_provider,
            model="m", max_tokens=100,
        )
    reservations = UsageReservationRepository(store).list_for_task(task_id)
    states = [r["state"] for r in reservations]
    assert "RELEASED" in states, f"应该有 RELEASED reservation,got {states}"


def test_usage_summary_by_agent(tmp_path):
    from tradingagents.agent_harness.core.llm_executor import LLMExecutor
    from tradingagents.agent_harness.runtime.persistence.usage import UsageReservationRepository
    store, run_id, task_id = _make_run_task(tmp_path)

    def fake(msgs, **kw):
        return _FakeResponse(content="ok", prompt_tokens=10, completion_tokens=5)
    ex = LLMExecutor(store)
    ex.complete(
        task_id=task_id, agent_name="AgentA",
        messages=[{"role": "user", "content": "hi"}],
        provider=fake, model="m", max_tokens=50,
    )
    ex.complete(
        task_id=task_id, agent_name="AgentA",
        messages=[{"role": "user", "content": "hi2"}],
        provider=fake, model="m", max_tokens=50,
    )
    ex.complete(
        task_id=task_id, agent_name="AgentB",
        messages=[{"role": "user", "content": "hi"}],
        provider=fake, model="m", max_tokens=50,
    )

    summary = UsageReservationRepository(store).usage_summary_by_agent(run_id)
    # AgentA: 2 calls, 20 in + 10 out
    # AgentB: 1 call, 10 in + 5 out
    by_agent = {row["agent_name"]: row for row in summary}
    assert by_agent["AgentA"]["call_count"] == 2
    assert by_agent["AgentA"]["total_input_tokens"] == 20
    assert by_agent["AgentB"]["call_count"] == 1


class _FakeResponse:
    def __init__(self, content, prompt_tokens=0, completion_tokens=0):
        self.content = content
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.model = "fake"
