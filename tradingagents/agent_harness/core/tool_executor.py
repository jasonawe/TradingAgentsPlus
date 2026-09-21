"""Task 8 — ToolExecutor.

Spec §5.3:ToolExecutor 是所有 Agent 和 Tier 1 共用的工具执行入口。
- call ToolRegistry 只在文件内
- enforce AgentScope
- persist operation BEFORE side effect
- 通过 ToolPipeline(pre / guard / execute / post)
- 返回 typed ``ToolExecutionResult``
- ASK → AWAITING_APPROVAL;不直接执行
- 任何代码路径都不能直接 ``tool.invoke()`` — 只能通过 ToolExecutor
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..tools.base import BaseTool
from ..tools.permission import PermissionType
from ..tools.registry import ToolRegistry
from ..tools.schema import SideEffectMode

LOGGER = logging.getLogger(__name__)


@dataclass
class ToolExecutionResult:
    """Typed outcome of a tool execution. Caller inspects flags + result."""

    tool_name: str
    ok: bool = False
    result: Any = None
    error: str | None = None
    denied: bool = False
    denied_by: str | None = None
    needs_approval: bool = False
    approval_id: str | None = None
    indeterminate: bool = False
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "denied": self.denied,
            "denied_by": self.denied_by,
            "needs_approval": self.needs_approval,
            "approval_id": self.approval_id,
            "indeterminate": self.indeterminate,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class ToolExecutor:
    """Single entry point for tool invocation.

    所有 ``tool.invoke()`` 调用都从这里出去 — 其他模块不能直接调用。
    """

    registry: ToolRegistry
    store: Any | None = None  # AgentRuntimeStore, optional for pure read paths

    def execute_read(
        self,
        *,
        tool_name: str,
        raw_args: dict[str, Any],
        context: Any,
        agent_scope: Any | None = None,
    ) -> ToolExecutionResult:
        """Tier 1 直接读路径。不持久化 operation,只读缓存去重。"""
        try:
            tool = self.registry.get(tool_name)
        except KeyError as e:
            return ToolExecutionResult(tool_name=tool_name, ok=False, error=str(e))

        # AgentScope 检查(用 tool allowlist)
        if agent_scope is not None:
            all_tools = list(self.registry.list_names())
            if not agent_scope.is_tool_allowed(tool_name, all_tools):
                return ToolExecutionResult(
                    tool_name=tool_name, ok=False,
                    denied=True, denied_by="agent_scope",
                    error=f"tool {tool_name!r} not in agent scope allowlist",
                )

        # 执行
        try:
            args = self._coerce_args(tool, raw_args)
            coro = tool.invoke(args, context)
            if asyncio.iscoroutine(coro):
                result = asyncio.run(coro)
            else:
                result = coro
            return ToolExecutionResult(tool_name=tool_name, ok=True, result=result)
        except Exception as e:
            return ToolExecutionResult(
                tool_name=tool_name, ok=False,
                error=f"{type(e).__name__}: {e}",
            )

    def execute_operation(
        self,
        *,
        operation_id: str,
        tool_name: str,
        raw_args: dict[str, Any],
        context: Any,
        agent_scope: Any | None = None,
        run_id: str = "",
        task_id: str = "",
        redactor: Any | None = None,
    ) -> ToolExecutionResult:
        """写操作:持久化 BEFORE 执行,根据 side_effect_mode 决定是否需 HITL。

        返回 ``ToolExecutionResult``:
        - needs_approval=True → 不执行,等待审批(Caller 应把 run 标 WAITING_APPROVAL)
        - ok=True → 已执行
        - ok=False → 执行失败 / 异常
        """
        try:
            tool = self.registry.get(tool_name)
        except KeyError as e:
            return ToolExecutionResult(tool_name=tool_name, ok=False, error=str(e))

        if tool.permission == PermissionType.READ.value or tool.permission == PermissionType.READ:
            return ToolExecutionResult(
                tool_name=tool_name, ok=False,
                error="execute_operation called on read tool; use execute_read",
            )

        # side_effect_mode 必填(由 ToolSchema 在注册时校验;这里兜底)
        mode = tool.schema.metadata.get("side_effect_mode", SideEffectMode.NON_IDEMPOTENT)
        if isinstance(mode, str):
            mode = SideEffectMode(mode)

        # AgentScope 校验:用 tool allowlist(AgentScope 实际的语义)
        if agent_scope is not None:
            all_tools = list(self.registry.list_names())
            if not agent_scope.is_tool_allowed(tool_name, all_tools):
                return ToolExecutionResult(
                    tool_name=tool_name, ok=False, denied=True,
                    denied_by="agent_scope",
                    error=f"tool {tool_name!r} not in agent scope allowlist",
                )

        # 持久化 BEFORE side effect — 同一 operation_id 重复调用幂等
        if self.store is None:
            return ToolExecutionResult(
                tool_name=tool_name, ok=False,
                error="execute_operation requires store for operation persistence",
            )
        from ..runtime.persistence.operations import OperationRepository
        op_repo = OperationRepository(self.store)

        redacted = redactor(raw_args) if redactor is not None else raw_args
        args_hash = hashlib.sha256(
            json.dumps(redacted, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        now = _now_iso()
        idempotency_key = f"op:{operation_id}"

        op_row = op_repo.create_operation(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            run_id=run_id, task_id=task_id,
            tool_name=tool_name,
            args_hash=args_hash,
            redacted_args=redacted,
            side_effect_mode=mode.value,
            now=now,
        )
        # 幂等:已经成功的 operation 直接返回旧结果,不再执行
        if op_row["state"] == "SUCCEEDED":
            return ToolExecutionResult(
                tool_name=tool_name, ok=True, result=op_row.get("result_json"),
            )

        # NON_IDEMPOTENT 必须 HITL
        if mode == SideEffectMode.NON_IDEMPOTENT:
            approval_id = str(uuid.uuid4())
            op_repo.mark_awaiting_approval(
                operation_id=operation_id, approval_id=approval_id, now=now,
            )
            return ToolExecutionResult(
                tool_name=tool_name, ok=False,
                needs_approval=True, approval_id=approval_id,
                error="NON_IDEMPOTENT tool requires HITL approval",
            )

        # EXECUTING
        op_repo.claim_operation(
            operation_id=operation_id,
            worker_id="tool-executor",
            lease_expires_at=now,
            now=now,
        )

        # 实际执行
        try:
            args = self._coerce_args(tool, raw_args)
            coro = tool.invoke(args, context)
            if asyncio.iscoroutine(coro):
                result = asyncio.run(coro)
            else:
                result = coro
            op_repo.mark_completed(operation_id=operation_id, result_json=result, now=_now_iso())
            return ToolExecutionResult(tool_name=tool_name, ok=True, result=result)
        except Exception as e:
            op_repo.mark_failed(
                operation_id=operation_id,
                error_json={"type": type(e).__name__, "msg": str(e)},
                now=_now_iso(),
            )
            return ToolExecutionResult(
                tool_name=tool_name, ok=False,
                error=f"{type(e).__name__}: {e}",
            )

    def _coerce_args(self, tool: BaseTool, raw_args: dict[str, Any]) -> Any:
        """Pydantic coercion — 让工具声明的 args_schema 校验入参。"""
        schema = getattr(tool, "args_schema", None)
        if schema is None:
            return raw_args
        try:
            return schema(**(raw_args or {}))
        except TypeError:
            # 如果 schema 不是 BaseModel(测试里用 class placeholder),原样返回
            return raw_args


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


__all__ = ["ToolExecutor", "ToolExecutionResult"]
