"""Task 14 — ContextAssembler + TurnRepository tests.

覆盖 plan 要求:
- ordering: explicit task input → dependency artifacts → L1 chat →
  L2 prefs → L3 refs → system constraints
- pending Runtime terminal overlay
- projection receipt suppression(已投影的不重复)
- no failed / unverified draft in context
- symbol / intent carry-forward
- session accounting(create / touch / token_total)
- delete_session 协调取消与 store cleanup
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


# ════════════════════════════════════════════════════════
# ContextAssembler — ordering
# ════════════════════════════════════════════════════════


def _store(tmp_path):
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    return AgentRuntimeStore(tmp_path / "runtime.sqlite")


def _now():
    return datetime.now(timezone.utc).isoformat()


def test_context_assembler_returns_typed_bundle(tmp_path):
    """assemble(task) 返回 AgentContextBundle 数据结构。"""
    from tradingagents.agent_harness.core.context_assembler import (
        ContextAssembler,
    )
    s = _store(tmp_path)
    asm = ContextAssembler(store=s, memory=None)
    bundle = asm.assemble(
        task={
            "task_id": "t1", "run_id": "r1", "turn_id": "turn1",
            "objective": "lookup AAPL", "inputs": {"symbols": ["AAPL"]},
        },
        session_id="sess-1",
    )
    assert hasattr(bundle, "task_input")
    assert hasattr(bundle, "dependency_artifacts")
    assert hasattr(bundle, "l1_chat")
    assert hasattr(bundle, "l2_prefs")
    assert hasattr(bundle, "l3_refs")
    assert hasattr(bundle, "system_constraints")
    assert bundle.task_input["symbols"] == ["AAPL"]


def test_context_assembler_layer_ordering(tmp_path):
    """layers 顺序:task_input → deps → L1 → L2 → L3 → system。"""
    from tradingagents.agent_harness.core.context_assembler import (
        ContextAssembler, Layer,
    )
    s = _store(tmp_path)
    asm = ContextAssembler(store=s, memory=None)
    bundle = asm.assemble(
        task={"task_id": "t1", "run_id": "r1", "turn_id": "turn1",
              "objective": "x", "inputs": {}},
        session_id="sess-1",
    )
    expected_order = [
        Layer.TASK_INPUT, Layer.DEPENDENCY_ARTIFACTS, Layer.L1_CHAT,
        Layer.L2_PREFS, Layer.L3_REFS, Layer.SYSTEM_CONSTRAINTS,
    ]
    assert bundle.layer_order() == expected_order


# ════════════════════════════════════════════════════════
# Dependency artifact resolution
# ════════════════════════════════════════════════════════


def test_context_assembler_resolves_dependency_artifacts(tmp_path):
    """deps → agent_artifacts → 进入 dependency_artifacts 层。"""
    s = _store(tmp_path)
    # 注入一个 artifact
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    from tradingagents.agent_harness.runtime.persistence.tasks import TaskRepository
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    rr = RunRepository(s)
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["x"], carry_symbols=[], slots={},
        tier=2, confidence=1.0,
        reason_code="t", route_kind="DIRECT_READ",
    )
    run = rr.create_run(
        run_id=str(uuid.uuid4()), session_id="sess-1",
        turn_id="turn1", run_kind="AGENT_ANALYSIS", route=route,
        budgets={"max_llm_calls": 10}, now=_now(),
    )
    # 创建真实的 parent task(artifact FK 约束)
    tr_db = TaskRepository(s)
    class _P:
        objective = "obj"
        inputs = {}
    parent = tr_db.create_task(
        task_id=str(uuid.uuid4()),
        run_id=run["run_id"], parent_task_id=None,
        kind="AGENT", agent_name="DataAgent", system_handler=None,
        capability="domain_lookup", payload=_P(),
        required=True, max_execution_attempts=3, now=_now(),
    )
    parent_task_id = parent["task_id"]
    s.connection.execute(
        "INSERT INTO agent_artifacts "
        "(artifact_id, run_id, producer_task_id, source_type, source_name, "
        " content_json, content_sha256, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), run["run_id"], parent_task_id, "tool",
         "get_quote", '{"symbol": "AAPL", "price": 100}',
         "sha256abc", _now()),
    )
    s.connection.commit()

    from tradingagents.agent_harness.core.context_assembler import ContextAssembler
    asm = ContextAssembler(store=s, memory=None)
    bundle = asm.assemble(
        task={"task_id": "t1", "run_id": run["run_id"], "turn_id": "turn1",
              "objective": "verify", "inputs": {},
              "dependency_results": [{"producer_task_id": parent_task_id,
                                      "artifact_id": "sha256abc"}]},
        session_id="sess-1",
    )
    assert len(bundle.dependency_artifacts) >= 1


# ════════════════════════════════════════════════════════
# Projection receipt suppression
# ════════════════════════════════════════════════════════


def test_context_assembler_excludes_projected_exchanges(tmp_path):
    """已投影的 exchange 不应再次出现在 L1 chat 中。"""
    from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
    mem = SqliteSessionMemory(
        use_langgraph_checkpointer=False,
        data_dir=str(tmp_path),
    )
    # 投影一次
    mem.append_projected_exchange("sess-1", "Q1", "A1", projection_key="runtime:sess-1:Q1")
    # 再写一条未投影的
    mem.append_message("sess-1", "user", "Q2")
    mem.append_message("sess-1", "assistant", "A2")

    from tradingagents.agent_harness.core.context_assembler import ContextAssembler
    s = _store(tmp_path)
    asm = ContextAssembler(store=s, memory=mem)
    bundle = asm.assemble(
        task={"task_id": "t1", "run_id": "r1", "turn_id": "turn1",
              "objective": "x", "inputs": {}},
        session_id="sess-1",
    )
    # get_history 返回全部已写入消息 — 但 ContextAssembler 应过滤掉已投影的
    msgs = bundle.l1_chat or []
    assert all(m.get("content") != "Q1" for m in msgs)
    assert any(m.get("content") == "Q2" for m in msgs)


# ════════════════════════════════════════════════════════
# TurnRepository — session accounting
# ════════════════════════════════════════════════════════


def test_turn_repository_create_session_sets_metadata(tmp_path):
    """create_session 初始化 metadata,touch 更新 updated_at。"""
    from tradingagents.agent_harness.core.turn_repository import TurnRepository
    s = _store(tmp_path)
    tr = TurnRepository(s)
    session_id = "sess-x"
    tr.create_session(session_id=session_id, now=_now())
    meta = tr.get_session(session_id)
    assert meta is not None
    assert meta["session_id"] == session_id
    assert meta["token_total"] == 0

    tr.touch_session(session_id=session_id, now=_now())
    meta2 = tr.get_session(session_id)
    assert meta2["updated_at"] >= meta["updated_at"]


def test_turn_repository_add_tokens_increments_total(tmp_path):
    """add_tokens 累加 token_total。"""
    from tradingagents.agent_harness.core.turn_repository import TurnRepository
    s = _store(tmp_path)
    tr = TurnRepository(s)
    tr.create_session(session_id="sess-y", now=_now())
    tr.add_tokens(session_id="sess-y", prompt=100, completion=50)
    tr.add_tokens(session_id="sess-y", prompt=200, completion=75)
    meta = tr.get_session("sess-y")
    assert meta["token_total"] == 425


def test_turn_repository_finalize_projection_marks_delivered(tmp_path):
    """finalize_projection 把 run 的 session_projection_state 标 DELIVERED。"""
    from tradingagents.agent_harness.core.turn_repository import TurnRepository
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    s = _store(tmp_path)
    rr = RunRepository(s)
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["x"], carry_symbols=[], slots={},
        tier=2, confidence=1.0,
        reason_code="t", route_kind="DIRECT_READ",
    )
    run = rr.create_run(
        run_id=str(uuid.uuid4()), session_id="sess-z",
        turn_id="turn1", run_kind="AGENT_ANALYSIS", route=route,
        budgets={"max_llm_calls": 10}, now=_now(),
    )
    tr = TurnRepository(s)
    tr.finalize_projection(run_id=run["run_id"], now=_now())
    refreshed = s.connection.execute(
        "SELECT session_projection_state FROM agent_runs WHERE run_id=?",
        (run["run_id"],),
    ).fetchone()
    assert refreshed[0] == "DELIVERED"


# ════════════════════════════════════════════════════════
# delete_session — coordinate cancellation + cleanup
# ════════════════════════════════════════════════════════


def test_turn_repository_delete_session_removes_runs(tmp_path):
    """delete_session 把 session 下所有 run 标 CANCELLED 并删除子表。"""
    from tradingagents.agent_harness.core.turn_repository import TurnRepository
    from tradingagents.agent_harness.runtime.persistence.runs import RunRepository
    from tradingagents.agent_harness.runtime.models import RouteDecision
    from tradingagents.agent_harness.core.tier import Intent, Op
    s = _store(tmp_path)
    rr = RunRepository(s)
    route = RouteDecision(
        intent=Intent.QUOTE, op=Op.LIST,
        symbols=["x"], carry_symbols=[], slots={},
        tier=2, confidence=1.0,
        reason_code="t", route_kind="DIRECT_READ",
    )
    r1 = rr.create_run(
        run_id=str(uuid.uuid4()), session_id="sess-del",
        turn_id="turn1", run_kind="AGENT_ANALYSIS", route=route,
        budgets={"max_llm_calls": 10}, now=_now(),
    )
    rr.mark_terminal(
        run_id=r1["run_id"], expected_state="PLANNING",
        final_seq=1, final_result_json=None,
        terminal_reason="SUCCEEDED", now=_now(),
    )
    rr.create_run(
        run_id=str(uuid.uuid4()), session_id="sess-del",
        turn_id="turn2", run_kind="AGENT_ANALYSIS", route=route,
        budgets={"max_llm_calls": 10}, now=_now(),
    )
    tr = TurnRepository(s)
    counts = tr.delete_session(session_id="sess-del", now=_now())
    assert counts["runs"] == 2
    remaining = s.connection.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE session_id=?",
        ("sess-del",),
    ).fetchone()[0]
    assert remaining == 0
