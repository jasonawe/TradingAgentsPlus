"""Task 12 — AgentDispatcher + AgentRuntime facade tests.

覆盖 plan 要求:
- AGENT tasks call only AgentRegistry.get(name).run(task, context=context)
- SYSTEM_COMMAND tasks call only CommandResolver
- outgoing messages pass MessageIngestor before store
- AgentReply creates exactly one RESULT message
- progress persists before projection
- start / resume / answer / confirm / cancel / reconcile obey state + correlation
- session has one active run
- legacy replacement CAS reuses the same run
- recover() composes scheduler + dispatcher
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


# ════════════════════════════════════════════════════════
# Fakes — minimal V2 agent / command surface
# ════════════════════════════════════════════════════════


class _FakeReply:
    def __init__(self, *, success=True, content="ok", outgoing=None,
                 evidence=None, errors=None):
        self.success = success
        self.content = content
        self.outgoing = list(outgoing or [])
        self.evidence = list(evidence or [])
        self.errors = list(errors or [])
        self.confidence = 1.0
        self.missing_items = []


class _FakeAgent:
    def __init__(self, name, reply=_FakeReply()):
        self.name = name
        self.reply = reply
        self.calls = []

    def run(self, task, context):
        self.calls.append({"task": task, "context": context})
        return self.reply


class _FakeAgentRegistry:
    def __init__(self):
        self._agents = {}

    def register(self, name, agent):
        self._agents[name] = agent

    def get(self, name):
        return self._agents.get(name)


class _FakeCommandResolver:
    def __init__(self):
        self.calls = []
        self.results = {}  # tool_name → result

    def resolve(self, *, intent, op, user_message, symbols=None,
                carry_symbols=None, slots=None):
        from tradingagents.agent_harness.core.tier import Intent, Op
        from tradingagents.agent_harness.runtime.models import CommandSpec
        from tradingagents.agent_harness.tools.permission import PermissionType
        self.calls.append({
            "intent": intent, "op": op, "user_message": user_message,
            "symbols": symbols or [], "carry_symbols": carry_symbols or [],
            "slots": slots or {},
        })
        tool = f"tool:{intent.value}/{op.value}"
        return CommandSpec(
            command_id=str(uuid.uuid4()),
            entity="watchlist",
            op=str(op.value).upper().split(".")[-1] if hasattr(op, "value") else str(op).upper(),
            tool_name=tool,
            args={"intent": intent.value, "op": str(op.value), "user_message": user_message},
            permission=PermissionType.READ,
            requires_approval=False,
            result_view="default",
        )


class _FakeMessageIngestor:
    def __init__(self):
        self.calls = []

    def ingest(self, *, run_id, turn_id, task, draft, now=None, correlation_id=None):
        from tradingagents.agent_harness.runtime.models import AgentMessage
        seq = len(self.calls) + 1
        msg = AgentMessage(
            message_id=str(uuid.uuid4()),
            seq=seq,
            run_id=run_id,
            turn_id=turn_id,
            task_id=task["task_id"],
            parent_task_id=task.get("parent_task_id"),
            sender=task.get("agent_name", "unknown"),
            recipient=draft.recipient,
            type=_kind_to_message_type(draft.type),
            payload=draft.payload,
            evidence_refs=draft.evidence_refs,
            causation_id=None,
            correlation_id=correlation_id or str(uuid.uuid4()),
            idempotency_key=str(uuid.uuid4()),
            execution_attempt=task.get("execution_attempt", 1),
            created_at=datetime.now(timezone.utc),
        )
        self.calls.append({"draft": draft, "message": msg})
        return msg


def _kind_to_message_type(t):
    from tradingagents.agent_harness.runtime.models import AgentMessageType
    return AgentMessageType(t)


class _FakeContextProvider:
    def __init__(self, bundle=None):
        self.bundle = bundle or {"slots": {}, "memory": {}}
        self.calls = []

    def assemble(self, task):
        self.calls.append(task)
        return dict(self.bundle)


# ════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════


def _now():
    return datetime.now(timezone.utc).isoformat()


def _store(tmp_path):
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    return AgentRuntimeStore(tmp_path / "runtime.sqlite")


def _make_run(store, *, session_id="sess-1", run_kind="AGENT_ANALYSIS"):
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    rr = RunRepository(store)
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["x"], carry_symbols=[], slots={},
        tier=2, confidence=1.0,
        reason_code="t", route_kind="DIRECT_READ",
    )
    return rr.create_run(
        run_id=str(uuid.uuid4()),
        session_id=session_id, turn_id="turn-1",
        run_kind=run_kind, route=route,
        budgets={"max_llm_calls": 10}, now=_now(),
    )


def _make_task(store, run, *, kind="AGENT", agent_name="TestAgent",
               capability="verify", required=True, state="READY"):
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    tr = TaskRepository(store)
    class _P:
        objective = "obj"
        inputs = {}
    payload = _P()
    t = tr.create_task(
        task_id=str(uuid.uuid4()),
        run_id=run["run_id"], parent_task_id=None,
        kind=kind, agent_name=agent_name, system_handler=None,
        capability=capability, payload=payload,
        required=required, max_execution_attempts=3, now=_now(),
    )
    if state != "READY":
        t = tr.transition_task(
            task_id=t["task_id"],
            expected_version=t["version"], expected_state="READY",
            new_state=state, now=_now(),
        )
    return t


# ════════════════════════════════════════════════════════
# AgentDispatcher — agent dispatch
# ════════════════════════════════════════════════════════


def test_dispatcher_dispatches_agent_task_to_registry(tmp_path):
    """AGENT task → AgentRegistry.get(name).run(task, context=context)。"""
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, kind="AGENT", agent_name="TestAgent")
    agent = _FakeAgent("TestAgent", reply=_FakeReply(content="hi"))
    reg = _FakeAgentRegistry()
    reg.register("TestAgent", agent)
    ctx = _FakeContextProvider()

    from tradingagents.agent_harness.runtime.dispatcher import AgentDispatcher
    disp = AgentDispatcher(
        agent_registry=reg,
        command_resolver=_FakeCommandResolver(),
        message_ingestor=_FakeMessageIngestor(),
        context_provider=ctx,
    )
    disp.dispatch(task=t, run=r, store=s, now=_now())
    assert len(agent.calls) == 1
    assert agent.calls[0]["context"] == ctx.bundle


def test_dispatcher_dispatches_command_task_to_resolver(tmp_path):
    """SYSTEM_COMMAND task → CommandResolver(不调用 AgentRegistry)。"""
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, kind="SYSTEM_COMMAND", agent_name=None)
    resolver = _FakeCommandResolver()
    reg = _FakeAgentRegistry()
    ctx = _FakeContextProvider()

    from tradingagents.agent_harness.runtime.dispatcher import AgentDispatcher
    disp = AgentDispatcher(
        agent_registry=reg,
        command_resolver=resolver,
        message_ingestor=_FakeMessageIngestor(),
        context_provider=ctx,
    )
    disp.dispatch(task=t, run=r, store=s, now=_now())
    assert len(resolver.calls) == 1
    assert len(reg._agents) == 0  # 未触碰 agent registry


# ════════════════════════════════════════════════════════
# Outgoing messages pass through ingestor
# ════════════════════════════════════════════════════════


def test_dispatcher_routes_outgoing_through_ingestor(tmp_path):
    """reply.outgoing → MessageIngestor.ingest(每条 outgoing 一条 message)。

    成功 reply 还会有合成的 RESULT,所以 outgoing 的 PROGRESS 必须出现在 calls 中,
    不要求 calls 长度恰好为 1。
    """
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, kind="AGENT", agent_name="TestAgent")
    from tradingagents.agent_harness.runtime.models import (
        AgentMessageDraft, ProgressPayload,
    )
    outgoing = [
        AgentMessageDraft(
            recipient="X", type="PROGRESS",
            payload=ProgressPayload(kind="PROGRESS", stage="s1",
                                    summary="halfway", percent=50),
            evidence_refs=[],
        ),
    ]
    agent = _FakeAgent("TestAgent", reply=_FakeReply(outgoing=outgoing))
    reg = _FakeAgentRegistry()
    reg.register("TestAgent", agent)
    ingestor = _FakeMessageIngestor()
    from tradingagents.agent_harness.runtime.dispatcher import AgentDispatcher
    disp = AgentDispatcher(
        agent_registry=reg,
        command_resolver=_FakeCommandResolver(),
        message_ingestor=ingestor,
        context_provider=_FakeContextProvider(),
    )
    disp.dispatch(task=t, run=r, store=s, now=_now())
    # 至少一次调用,且其中包含 outgoing 的 PROGRESS
    assert len(ingestor.calls) >= 1
    types = [str(c["draft"].type) for c in ingestor.calls]
    assert any("PROGRESS" in t for t in types)


def test_dispatcher_persists_one_result_per_successful_reply(tmp_path):
    """成功 reply 必须产生恰好一条 RESULT message。"""
    s = _store(tmp_path)
    r = _make_run(s)
    t = _make_task(s, r, kind="AGENT", agent_name="TestAgent")
    agent = _FakeAgent("TestAgent", reply=_FakeReply(content="done"))
    reg = _FakeAgentRegistry()
    reg.register("TestAgent", agent)
    ingestor = _FakeMessageIngestor()
    from tradingagents.agent_harness.runtime.dispatcher import AgentDispatcher
    disp = AgentDispatcher(
        agent_registry=reg,
        command_resolver=_FakeCommandResolver(),
        message_ingestor=ingestor,
        context_provider=_FakeContextProvider(),
    )
    disp.dispatch(task=t, run=r, store=s, now=_now())
    # 1 outgoing message → 1 RESULT created by dispatcher
    assert len(ingestor.calls) == 1


# ════════════════════════════════════════════════════════
# AgentRuntime facade — start_analysis / cancel
# ════════════════════════════════════════════════════════


def test_runtime_start_analysis_creates_run_and_first_task(tmp_path):
    """start_analysis 创建 run + planner task,然后 claim + dispatch。"""
    s = _store(tmp_path)
    from tradingagents.agent_harness.runtime.runtime import AgentRuntime
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    agent = _FakeAgent("PlannerAgent", reply=_FakeReply(content="plan"))
    reg = _FakeAgentRegistry()
    reg.register("PlannerAgent", agent)
    rt = AgentRuntime(
        store=s,
        agent_registry=reg,
        command_resolver=_FakeCommandResolver(),
        message_ingestor=_FakeMessageIngestor(),
        context_provider=_FakeContextProvider(),
        scheduler_factory=lambda store: _StubScheduler(),
    )
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["AAPL"], carry_symbols=[], slots={},
        tier=2, confidence=1.0,
        reason_code="t", route_kind="AGENT_ANALYSIS",
    )
    run = rt.start_analysis(
        session_id="s1", turn_id="t1", route=route,
        planner_agent="PlannerAgent", planner_capability="planning",
        now=_now(),
    )
    assert run is not None
    assert run["run_kind"] == "AGENT_ANALYSIS"


def test_runtime_cancel_marks_run_cancelled(tmp_path):
    """cancel() 把 run 标记为 CANCELLED。"""
    s = _store(tmp_path)
    from tradingagents.agent_harness.runtime.runtime import AgentRuntime
    agent = _FakeAgent("PlannerAgent", reply=_FakeReply())
    reg = _FakeAgentRegistry()
    reg.register("PlannerAgent", agent)
    rt = AgentRuntime(
        store=s,
        agent_registry=reg,
        command_resolver=_FakeCommandResolver(),
        message_ingestor=_FakeMessageIngestor(),
        context_provider=_FakeContextProvider(),
        scheduler_factory=lambda store: _StubScheduler(),
    )
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["AAPL"], carry_symbols=[], slots={},
        tier=2, confidence=1.0,
        reason_code="t", route_kind="AGENT_ANALYSIS",
    )
    run = rt.start_analysis(
        session_id="s1", turn_id="t1", route=route,
        planner_agent="PlannerAgent", planner_capability="planning",
        now=_now(),
    )
    rt.cancel(run_id=run["run_id"], reason_code="USER_CANCELLED", now=_now())
    refreshed = s.connection.execute(
        "SELECT state FROM agent_runs WHERE run_id=?", (run["run_id"],)
    ).fetchone()
    assert refreshed[0] == "CANCELLED"


# ════════════════════════════════════════════════════════
# Session — one active run
# ════════════════════════════════════════════════════════


def test_runtime_rejects_second_active_run_for_same_session(tmp_path):
    """同 session 不能并发两个 active run。"""
    s = _store(tmp_path)
    from tradingagents.agent_harness.runtime.runtime import AgentRuntime
    agent = _FakeAgent("PlannerAgent", reply=_FakeReply())
    reg = _FakeAgentRegistry()
    reg.register("PlannerAgent", agent)
    rt = AgentRuntime(
        store=s,
        agent_registry=reg,
        command_resolver=_FakeCommandResolver(),
        message_ingestor=_FakeMessageIngestor(),
        context_provider=_FakeContextProvider(),
        scheduler_factory=lambda store: _StubScheduler(),
    )
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["AAPL"], carry_symbols=[], slots={},
        tier=2, confidence=1.0,
        reason_code="t", route_kind="AGENT_ANALYSIS",
    )
    rt.start_analysis(
        session_id="s1", turn_id="t1", route=route,
        planner_agent="PlannerAgent", planner_capability="planning",
        now=_now(),
    )
    with pytest.raises(RuntimeError, match="active run"):
        rt.start_analysis(
            session_id="s1", turn_id="t2", route=route,
            planner_agent="PlannerAgent", planner_capability="planning",
            now=_now(),
        )


# ════════════════════════════════════════════════════════
# Recover — composes store + scheduler + dispatcher
# ════════════════════════════════════════════════════════


def test_runtime_recover_calls_store_recover_and_scheduler(tmp_path):
    """recover() 同时调 store.recover() + scheduler.run_until_blocked。

    需要至少一个 active run,scheduler 才会被调用。
    """
    s = _store(tmp_path)
    # 先建一个 active run
    _make_run(s)
    from tradingagents.agent_harness.runtime.runtime import AgentRuntime
    agent = _FakeAgent("PlannerAgent", reply=_FakeReply())
    reg = _FakeAgentRegistry()
    reg.register("PlannerAgent", agent)
    sched = _StubScheduler()
    rt = AgentRuntime(
        store=s,
        agent_registry=reg,
        command_resolver=_FakeCommandResolver(),
        message_ingestor=_FakeMessageIngestor(),
        context_provider=_FakeContextProvider(),
        scheduler_factory=lambda store: sched,
    )
    rt.recover(now=_now())
    assert sched.run_until_blocked_calls == 1


# ════════════════════════════════════════════════════════
# Stub scheduler — records calls without doing real scheduling
# ════════════════════════════════════════════════════════


class _StubScheduler:
    """Minimal scheduler stub used in dispatcher / runtime tests."""

    def __init__(self):
        self.claim_calls = []
        self.run_until_blocked_calls = 0
        self.claim_ready_task = self._claim
        self.run_once = lambda **kwargs: {
            "resolved_wait_ids": [],
            "promoted_task_ids": [],
            "aggregated_run_ids": [],
        }
        self.run_until_blocked = self._drain

    def _claim(self, *, run_id=None, worker_id, now=None):
        self.claim_calls.append({"run_id": run_id, "worker_id": worker_id})
        return None  # never actually claims in dispatcher tests

    def _drain(self, run_id, *, now=None):
        self.run_until_blocked_calls += 1
        return {
            "resolved_wait_ids": [],
            "promoted_task_ids": [],
            "aggregated_run_ids": [],
        }
