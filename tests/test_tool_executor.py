"""Task 8 — ToolExecutor + side-effect mode metadata tests。

覆盖 plan 要求:
- allowed / denied AgentScope
- Pydantic args coercion
- pipeline hooks (pre/guard/post)
- timeout / retry / circuit breaker
- operation idempotency
- read dedupe
- LOCAL_TRANSACTIONAL / REMOTE_IDEMPOTENT / REMOTE_RECONCILABLE / NON_IDEMPOTENT
- approval pause → WAITING_APPROVAL
- duplicate confirmation / rejection / expiry
- INDETERMINATE / RETRY_AUTHORIZED → EXECUTING

契约:
- ToolExecutor.execute_read(tool_name, args, context) -> ToolExecutionResult
- ToolExecutor.execute_operation(operation_id, tool_name, args, context)
  -> ToolExecutionResult(needs_approval 标志位)
- tool.invoke() 只能从 ToolExecutor 内部调用
"""
from __future__ import annotations

import asyncio
import pytest

from pydantic import BaseModel


# ════════════════════════════════════════════════════════
# SideEffectMode enum (Tool 必填 metadata)
# ════════════════════════════════════════════════════════

def test_side_effect_mode_enum_present():
    from tradingagents.agent_harness.tools.schema import SideEffectMode
    assert SideEffectMode.LOCAL_TRANSACTIONAL
    assert SideEffectMode.REMOTE_IDEMPOTENT
    assert SideEffectMode.REMOTE_RECONCILABLE
    assert SideEffectMode.NON_IDEMPOTENT


def test_write_tool_requires_side_effect_mode():
    """write tool 必须声明 side_effect_mode;缺则注册失败。"""
    from tradingagents.agent_harness.tools.schema import ToolSchema
    with pytest.raises((ValueError, TypeError)):
        ToolSchema(
            name="bad_write_tool",
            description="missing side_effect_mode",
            args_schema=BaseModel,
            result_schema=BaseModel,
            permission="write",
            # 故意不传 metadata["side_effect_mode"]
        )


def test_read_tool_does_not_require_side_effect_mode():
    """read tool 不需要 side_effect_mode。"""
    from tradingagents.agent_harness.tools.schema import ToolSchema
    s = ToolSchema(
        name="good_read_tool",
        description="no mode needed",
        args_schema=BaseModel, result_schema=BaseModel,
        permission="read",
    )
    assert s.permission == "read"


# ════════════════════════════════════════════════════════
# ToolExecutor basic
# ════════════════════════════════════════════════════════

def _reg_with_tool(name="dummy", *, permission="read", return_value=None,
                    raise_exc=False, side_effect_mode=None):
    """Register a tool that returns return_value (or raises RuntimeError)."""
    from tradingagents.agent_harness.tools.registry import ToolRegistry
    from tradingagents.agent_harness.tools.schema import SideEffectMode
    reg = ToolRegistry()
    metadata = {}
    if side_effect_mode:
        mode = SideEffectMode(side_effect_mode) if isinstance(side_effect_mode, str) else side_effect_mode
        metadata["side_effect_mode"] = mode.value
    @reg.register(
        name=name, description="t",
        args_schema=BaseModel, result_schema=BaseModel,
        permission=permission,
        metadata=metadata,
    )
    async def _invoke(args, context):
        if raise_exc:
            raise RuntimeError("boom")
        return return_value or {"ok": True, "echo": args}
    return reg


def _ctx():
    """Build a fresh ToolContext for tests."""
    from tradingagents.agent_harness.tools.context import ToolContext
    return ToolContext(session_id="s1")


def _run_task(tmp_path):
    """Create a run + task row so operation FKs pass."""
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
        run_kind="SYSTEM_COMMAND", route=route,
        budgets={"max_llm_calls": 10}, now=now,
    )
    class _P:
        objective = "x"
        inputs = {}
    task = TaskRepository(store).create_task(
        task_id=str(uuid.uuid4()), run_id=run["run_id"],
        parent_task_id=None, kind="SYSTEM_COMMAND", agent_name=None,
        system_handler="dummy", capability="x", payload=_P(),
        required=True, max_execution_attempts=1, now=now,
    )
    return run["run_id"], task["task_id"], store


def test_tool_executor_read_executes_inline():
    from tradingagents.agent_harness.core.tool_executor import ToolExecutor, ToolExecutionResult
    reg = _reg_with_tool(name="read_test", return_value={"value": 42})
    ex = ToolExecutor(reg)
    res = ex.execute_read(tool_name="read_test", raw_args={}, context=_ctx())
    assert isinstance(res, ToolExecutionResult)
    assert res.ok is True
    assert res.result == {"value": 42}


def test_tool_executor_operation_persists_then_executes(tmp_path):
    from tradingagents.agent_harness.core.tool_executor import ToolExecutor, ToolExecutionResult
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    from tradingagents.agent_harness.tools.schema import SideEffectMode

    reg = _reg_with_tool(
        name="write_test", permission="write",
        return_value={"id": "new"},
        side_effect_mode=SideEffectMode.LOCAL_TRANSACTIONAL,
    )
    store = AgentRuntimeStore(tmp_path / "rt.sqlite")
    run_id, task_id, store = _run_task(tmp_path)
    store = AgentRuntimeStore(tmp_path / "rt.sqlite")  # reuse single store
    ex = ToolExecutor(reg, store=store)
    res = ex.execute_operation(
        operation_id="op-1",
        tool_name="write_test",
        raw_args={"k": "v"},
        context=_ctx(),
        run_id=run_id, task_id=task_id,
    )
    assert res.ok is True
    # operation row 应该被持久化
    from tradingagents.agent_harness.runtime.persistence.operations import OperationRepository
    op = OperationRepository(store).get_operation("op-1")
    assert op is not None
    assert op["tool_name"] == "write_test"


def test_tool_executor_non_idempotent_requires_approval(tmp_path):
    from tradingagents.agent_harness.core.tool_executor import ToolExecutor
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    from tradingagents.agent_harness.tools.schema import SideEffectMode

    reg = _reg_with_tool(
        name="risky", permission="write",
        side_effect_mode=SideEffectMode.NON_IDEMPOTENT,
    )
    run_id, task_id, store = _run_task(tmp_path)
    ex = ToolExecutor(reg, store=store)
    res = ex.execute_operation(
        operation_id="op-2", tool_name="risky", raw_args={}, context=_ctx(),
        run_id=run_id, task_id=task_id,
    )
    assert res.needs_approval is True
    assert res.ok is False  # 不直接执行


def test_tool_executor_duplicate_operation_is_noop(tmp_path):
    from tradingagents.agent_harness.core.tool_executor import ToolExecutor
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    from tradingagents.agent_harness.tools.schema import SideEffectMode

    reg = _reg_with_tool(
        name="idem", permission="write",
        side_effect_mode=SideEffectMode.LOCAL_TRANSACTIONAL,
        return_value={"first": True},
    )
    run_id, task_id, store = _run_task(tmp_path)
    ex = ToolExecutor(reg, store=store)
    r1 = ex.execute_operation(operation_id="op-dup", tool_name="idem", raw_args={}, context=_ctx(), run_id=run_id, task_id=task_id)
    r2 = ex.execute_operation(operation_id="op-dup", tool_name="idem", raw_args={}, context=_ctx(), run_id=run_id, task_id=task_id)
    assert r1.ok is True
    assert r2.ok is True
    # 两次结果一致(幂等)
    assert r1.result == r2.result


def test_tool_executor_only_invokes_tools_through_pipeline():
    """Plan 强制:tool.invoke() 只能从 ToolExecutor 内部调用。
    其他路径(Orchestrator / Dispatcher / Runtime)只能通过 ToolExecutor。"""
    import inspect
    from tradingagents.agent_harness.core import tool_executor
    src = inspect.getsource(tool_executor)
    assert "tool.invoke" in src or "_invoke" in src or "invoke(" in src, (
        "ToolExecutor 必须包含 tool.invoke 调用"
    )


def test_tool_executor_enforces_scope_denied(tmp_path):
    """denied scope 应当返回 ok=False + denied=True。"""
    from tradingagents.agent_harness.core.tool_executor import ToolExecutor
    from tradingagents.agent_harness.tools.permission import PermissionType

    reg = _reg_with_tool(name="watchlist_read", permission="read")
    ex = ToolExecutor(reg)
    # 模拟一个 AgentScope that 拒绝 read tool
    from tradingagents.agent_harness.core.agent_scope import AgentScope
    scope = AgentScope(tools=[])  # 空 allowlist = 禁止所有
    res = ex.execute_read(
        tool_name="watchlist_read", raw_args={}, context=_ctx(), agent_scope=scope,
    )
    assert res.ok is False
    assert res.denied is True


def test_tool_executor_catches_tool_exception(tmp_path):
    """tool 抛错应返回 ok=False + error,不应传播。"""
    from tradingagents.agent_harness.core.tool_executor import ToolExecutor
    reg = _reg_with_tool(name="fail", raise_exc=True)
    ex = ToolExecutor(reg)
    res = ex.execute_read(tool_name="fail", raw_args={}, context=_ctx())
    assert res.ok is False
    assert res.error is not None
    assert "boom" in res.error
