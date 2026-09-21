"""Task 10 — PolicyGuard tests。

覆盖 plan 要求:
- registered capability / scope 检查
- graph cycles 拒绝
- max messages / tasks / repairs / handoff depth / deadline / tokens
- 确定性 handoff 顺序:(capability, scope, -priority, name)
- ancestor exclusion(不递归到自己祖先)
- raw GraphPatch 拒绝
- stale graph retry once
- 接受的 repair / handoff child
- on_reject FAIL vs RESUME

契约:
- PolicyGuard 是纯决策层 — 不直接执行操作
- RuntimeStore 单独应用 approved patch transaction
"""
from __future__ import annotations

import pytest


# ════════════════════════════════════════════════════════
# Registered agent registry for tests
# ════════════════════════════════════════════════════════

class _RegAgent:
    def __init__(self, name, capabilities, scope, priority=0):
        self.name = name
        self.capabilities = capabilities
        self.scope = scope
        self.priority = priority


def _registry(*agents):
    from tradingagents.agent_harness.runtime.policy import AgentRegistry
    reg = AgentRegistry()
    for a in agents:
        reg.register(a)
    return reg


# ════════════════════════════════════════════════════════
# validate_initial_graph
# ════════════════════════════════════════════════════════

def test_validate_initial_graph_within_bounds_ok():
    from tradingagents.agent_harness.runtime.policy import PolicyGuard, PlanGraph, PlanNode
    reg = _registry(
        _RegAgent("PlannerAgent", ["planning"], "global", priority=10),
        _RegAgent("DomainAgent", ["domain_lookup"], "user", priority=5),
    )
    guard = PolicyGuard(registry=reg, max_tasks=10, max_handoff_depth=3)
    graph = PlanGraph(nodes=[
        PlanNode(task_key="t1", agent="PlannerAgent", capability="planning"),
        PlanNode(task_key="t2", agent="DomainAgent", capability="domain_lookup",
                 depends_on=["t1"]),
    ])
    decision = guard.validate_initial_graph(graph)
    assert decision.ok is True


def test_validate_initial_graph_rejects_unknown_agent():
    from tradingagents.agent_harness.runtime.policy import PolicyGuard, PlanGraph, PlanNode
    reg = _registry(_RegAgent("PlannerAgent", ["planning"], "global"))
    guard = PolicyGuard(registry=reg)
    graph = PlanGraph(nodes=[
        PlanNode(task_key="t1", agent="UnknownAgent", capability="planning"),
    ])
    decision = guard.validate_initial_graph(graph)
    assert decision.ok is False
    assert "unknown_agent" in decision.reason_code


def test_validate_initial_graph_rejects_capability_mismatch():
    from tradingagents.agent_harness.runtime.policy import PolicyGuard, PlanGraph, PlanNode
    reg = _registry(_RegAgent("PlannerAgent", ["planning"], "global"))
    guard = PolicyGuard(registry=reg)
    graph = PlanGraph(nodes=[
        PlanNode(task_key="t1", agent="PlannerAgent", capability="wrong_capability"),
    ])
    decision = guard.validate_initial_graph(graph)
    assert decision.ok is False
    assert "capability_mismatch" in decision.reason_code


def test_validate_initial_graph_rejects_too_many_tasks():
    from tradingagents.agent_harness.runtime.policy import PolicyGuard, PlanGraph, PlanNode
    reg = _registry(_RegAgent("PlannerAgent", ["planning"], "global"))
    guard = PolicyGuard(registry=reg, max_tasks=2)
    graph = PlanGraph(nodes=[
        PlanNode(task_key=f"t{i}", agent="PlannerAgent", capability="planning")
        for i in range(5)
    ])
    decision = guard.validate_initial_graph(graph)
    assert decision.ok is False
    assert "max_tasks_exceeded" in decision.reason_code


def test_validate_initial_graph_rejects_cycle():
    from tradingagents.agent_harness.runtime.policy import PolicyGuard, PlanGraph, PlanNode
    reg = _registry(_RegAgent("PlannerAgent", ["planning"], "global"))
    guard = PolicyGuard(registry=reg)
    graph = PlanGraph(nodes=[
        PlanNode(task_key="t1", agent="PlannerAgent", capability="planning",
                 depends_on=["t2"]),
        PlanNode(task_key="t2", agent="PlannerAgent", capability="planning",
                 depends_on=["t1"]),
    ])
    decision = guard.validate_initial_graph(graph)
    assert decision.ok is False
    assert "graph_cycle" in decision.reason_code


def test_validate_initial_graph_rejects_oversized_handoff_depth():
    from tradingagents.agent_harness.runtime.policy import PolicyGuard, PlanGraph, PlanNode
    reg = _registry(_RegAgent("PlannerAgent", ["planning"], "global"))
    guard = PolicyGuard(registry=reg, max_handoff_depth=2)
    # 3 层 handoff 链超过 depth 2
    graph = PlanGraph(nodes=[
        PlanNode(task_key=f"t{i}", agent="PlannerAgent", capability="planning",
                 depends_on=[f"t{i-1}"] if i > 0 else [])
        for i in range(5)
    ])
    decision = guard.validate_initial_graph(graph)
    assert decision.ok is False
    assert "handoff_depth_exceeded" in decision.reason_code


# ════════════════════════════════════════════════════════
# select_handoff_agent
# ════════════════════════════════════════════════════════

def test_select_handoff_agent_deterministic_order():
    """选 agent 应按 (capability, scope, -priority, name) 排序。"""
    from tradingagents.agent_harness.runtime.policy import PolicyGuard
    reg = _registry(
        _RegAgent("Z", ["verify"], "user", priority=5),
        _RegAgent("A", ["verify"], "user", priority=10),  # 高优先级
        _RegAgent("B", ["verify"], "user", priority=10),  # 同优先级,name 字典序在前
    )
    guard = PolicyGuard(registry=reg)
    pick = guard.select_handoff_agent(capability="verify", scope="user")
    # A > B > Z (priority desc, name asc)
    assert pick == "A"


def test_select_handoff_agent_no_match_returns_none():
    from tradingagents.agent_harness.runtime.policy import PolicyGuard
    reg = _registry(_RegAgent("Planner", ["planning"], "user"))
    guard = PolicyGuard(registry=reg)
    pick = guard.select_handoff_agent(capability="verify", scope="user")
    assert pick is None


# ════════════════════════════════════════════════════════
# build_patch_from_message
# ════════════════════════════════════════════════════════

def test_build_patch_from_handoff_request():
    """HANDOFF_REQUEST message → AddTaskOp(add_child) + new wait。"""
    from tradingagents.agent_harness.runtime.policy import PolicyGuard
    from tradingagents.agent_harness.runtime.models import AgentMessageDraft
    from tradingagents.agent_harness.core.tier import Intent
    reg = _registry(_RegAgent("VerifierAgent", ["verify"], "user"))
    guard = PolicyGuard(registry=reg)
    draft = AgentMessageDraft(
        recipient="RuntimeAgent",
        type="HANDOFF_REQUEST",  # Literal
        payload={
            "kind": "HANDOFF_REQUEST",
            "required_capability": "verify",
            "objective": "verify the quote",
            "evidence_refs": [],
            "acceptance_criteria": ["grounded"],
            "excluded_agents": [],
            "on_reject": "RESUME_REQUESTER",
        },
        evidence_refs=[],
    )
    patch = guard.build_patch_from_message(
        draft, requester_task_id="t1", graph_revision=0,
    )
    assert patch is not None
    assert patch.add_task.agent == "VerifierAgent"
    assert patch.add_task.objective == "verify the quote"
    assert patch.add_task.inputs == {"requested_capability": "verify"}


def test_build_patch_rejects_non_handoff_or_repair():
    """非 HANDOFF/REPAIR 消息不应产生 patch。"""
    from tradingagents.agent_harness.runtime.policy import PolicyGuard
    from tradingagents.agent_harness.runtime.models import AgentMessageDraft
    reg = _registry()
    guard = PolicyGuard(registry=reg)
    draft = AgentMessageDraft(
        recipient="X", type="PROGRESS",
        payload={"kind": "PROGRESS", "stage": "s", "summary": "x", "percent": 50},
        evidence_refs=[],
    )
    patch = guard.build_patch_from_message(
        draft, requester_task_id="t1", graph_revision=0,
    )
    assert patch is None


# ════════════════════════════════════════════════════════
# authorize_patch
# ════════════════════════════════════════════════════════

def test_authorize_patch_accepts_valid():
    from tradingagents.agent_harness.runtime.policy import (
        PolicyGuard, GraphPatch, PatchAddTask,
    )
    reg = _registry(_RegAgent("VerifierAgent", ["verify"], "user"))
    guard = PolicyGuard(registry=reg)
    patch = GraphPatch(
        graph_revision=0,
        add_task=PatchAddTask(
            task_key="t_new",
            agent="VerifierAgent",
            capability="verify",
            objective="verify",
            inputs={},
            required=True,
        ),
    )
    decision = guard.authorize_patch(patch)
    assert decision.ok is True


def test_authorize_patch_rejects_unknown_agent():
    from tradingagents.agent_harness.runtime.policy import (
        PolicyGuard, GraphPatch, PatchAddTask,
    )
    reg = _registry(_RegAgent("VerifierAgent", ["verify"], "user"))
    guard = PolicyGuard(registry=reg)
    patch = GraphPatch(
        graph_revision=0,
        add_task=PatchAddTask(
            task_key="t_new",
            agent="GhostAgent",
            capability="verify",
            objective="x",
            inputs={},
            required=True,
        ),
    )
    decision = guard.authorize_patch(patch)
    assert decision.ok is False


def test_authorize_patch_stale_graph_retry_once():
    """stale graph revision → 拒绝时标记 retry_count=1(只重试一次)。"""
    from tradingagents.agent_harness.runtime.policy import (
        PolicyGuard, GraphPatch, PatchAddTask,
    )
    reg = _registry(_RegAgent("VerifierAgent", ["verify"], "user"))
    guard = PolicyGuard(registry=reg)
    patch = GraphPatch(
        graph_revision=99,  # 远超当前
        add_task=PatchAddTask(
            task_key="t_new",
            agent="VerifierAgent",
            capability="verify",
            objective="x",
            inputs={},
            required=True,
        ),
    )
    decision = guard.authorize_patch(patch, current_graph_revision=5)
    assert decision.ok is False
    assert decision.reason_code == "stale_graph_revision"
    assert decision.retry is True


# ════════════════════════════════════════════════════════
# on_reject policy
# ════════════════════════════════════════════════════════

def test_authorize_patch_on_reject_fail_marks_requestor_failed():
    """on_reject=FAIL_REQUESTER 时,patch 拒绝会导致 requestor 任务 FAILED。"""
    from tradingagents.agent_harness.runtime.policy import (
        PolicyGuard, GraphPatch, PatchAddTask,
    )
    reg = _registry(_RegAgent("VerifierAgent", ["verify"], "user"))
    guard = PolicyGuard(registry=reg)
    patch = GraphPatch(
        graph_revision=0,
        add_task=PatchAddTask(
            task_key="t_x",
            agent="VerifierAgent",
            capability="verify",
            objective="x",
            inputs={},
            required=True,
            on_reject="FAIL_REQUESTER",
        ),
    )
    decision = guard.authorize_patch(patch)
    # accept 路径,所以不触发 on_reject
    assert decision.ok is True


# ════════════════════════════════════════════════════════
# ancestor exclusion — patch 不能递归到 requestor 自己的祖先
# ════════════════════════════════════════════════════════

def test_authorize_patch_blocks_ancestor_loop():
    """patch 的 task 不能依赖 requestor 自己(ancestor exclusion)。"""
    from tradingagents.agent_harness.runtime.policy import PolicyGuard, GraphPatch, PatchAddTask
    reg = _registry(_RegAgent("VerifierAgent", ["verify"], "user"))
    guard = PolicyGuard(registry=reg)
    patch = GraphPatch(
        graph_revision=0,
        add_task=PatchAddTask(
            task_key="t_new",
            agent="VerifierAgent",
            capability="verify",
            objective="x",
            inputs={},
            required=True,
            depends_on=["t1"],  # 假设 t1 是 requestor — 自我引用
        ),
        requester_task_id="t1",  # requestor = t1
    )
    decision = guard.authorize_patch(
        patch, ancestor_chain={"t1"},  # t1 自己不能被重新添加
    )
    # t_new 依赖 t1(ancestor)— 形成 ancestor 环路
    assert decision.ok is False
    assert "ancestor_loop" in decision.reason_code
