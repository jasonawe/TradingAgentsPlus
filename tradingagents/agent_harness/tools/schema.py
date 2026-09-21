"""Tool schema contracts (v3 spec §5.2)."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class RetryPolicy(BaseModel):
    """Retry policy applied per-tool when the call raises a transient error."""

    max_retries: int = 2
    backoff_seconds: float = 0.5
    exponential: bool = True


class SideEffectMode(str, Enum):
    """Tool 调用副作用分类 — 决定 operation 状态机。

    - LOCAL_TRANSACTIONAL:本地 SQLite 写,成功即落库(常见 CRUD)
    - REMOTE_IDEMPOTENT:远程 API 有幂等键(idempotency_key),失败可重试
    - REMOTE_RECONCILABLE:远程 API 需要 reconcile 确认(银行/券商类)
    - NON_IDEMPOTENT:无幂等保证 — 必须 HITL 审批
    """
    LOCAL_TRANSACTIONAL = "local_transactional"
    REMOTE_IDEMPOTENT = "remote_idempotent"
    REMOTE_RECONCILABLE = "remote_reconcilable"
    NON_IDEMPOTENT = "non_idempotent"


class ToolSchema(BaseModel):
    """Standard contract every tool must declare.

    Step 23 (D2 Tool refactor) — extended surface:

    - ``metadata`` is the unified bag for capability tags, the
      user-facing display_view hint, lifecycle hook names, error
      normalization hints, etc. The harness reads
      ``metadata["capabilities"]`` / ``metadata["display_view"]``
      but the bag is open so individual callers can add keys without
      schema churn.

    Task 8:write tools MUST declare ``metadata["side_effect_mode"]`` —
    ToolExecutor 用它决定是否需要持久化 operation + 是否需要 HITL。
    """

    name: str
    description: str
    args_schema: type
    result_schema: type
    permission: str = "read"
    timeout_seconds: float = 30.0
    cache_ttl_seconds: int = 60
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    @model_validator(mode="after")
    def _write_requires_side_effect_mode(self) -> "ToolSchema":
        # 写操作必须声明副作用模式(spec §5.2 — 没有它 ToolExecutor 不知道
        # 是否需要持久化 operation / 是否需要 HITL)
        if self.permission == "write" and "side_effect_mode" not in self.metadata:
            raise ValueError(
                f"write tool {self.name!r} must declare metadata['side_effect_mode'] "
                f"(one of {[m.value for m in SideEffectMode]})"
            )
        return self
