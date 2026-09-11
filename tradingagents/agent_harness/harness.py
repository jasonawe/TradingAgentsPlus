"""Harness 主类 — 组装所有组件,提供统一入口。

P1 阶段:最小实现 — 只包含构造 + import 验证。
P2-P7 阶段:逐步填充组件(orchestrator / tier / verification / agents / plugins)。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncIterator

from tradingagents.agent_harness.config.schema import HarnessConfig

LOGGER = logging.getLogger(__name__)


class Harness:
    """Harness 主类 — Day 11e P1 最小实现。

    后续 Phase 填充:
    - P2: data providers (PROVIDERS dict)
    - P3: tool_registry + Tool Pydantic schemas
    - P4: orchestrator (StateGraph 5 nodes) + tier router + short_circuit
    - P5: agent_registry (5 sub-agents)
    - P6: plugin_registry + entry_points discovery
    - P7: observability (health / audit / circuit_breaker)
    """

    def __init__(self, config: HarnessConfig | None = None):
        self.config = config or HarnessConfig.from_env()
        LOGGER.info(
            "Harness initialized (data_dir=%s, llm_provider=%s, stage=P1-stub)",
            self.config.data_dir, self.config.llm_provider,
        )
        # Placeholder components — filled in P2-P7
        self.tool_registry = None
        self.agent_registry = None
        self.data_providers = {}
        self.audit = None
        self.health = None

    async def stream_chat(
        self,
        session_id: str,
        user_message: str,
        *,
        history: list | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """统一入口 — P4 才完整实现,P1 抛 NotImplementedError。

        web/routes/agent.py 暂时仍调旧 tradingagents.agents.general.build_agent
        等 P4 完成 StateGraph 后切换。
        """
        # 这里是 async generator function (有 yield 关键字),所以调用方用 async for
        # P1 阶段立即抛 NotImplementedError — unreachable yield 仅满足类型签名
        # TODO(P4): replace with orchestrator.stream_chat
        raise NotImplementedError(  # noqa: F904
            "Harness.stream_chat not implemented yet — will land in P4 (Day 13). "
            "Old code path: tradingagents.agents.general.build_agent"
        )
        yield  # unreachable: makes this an async generator function

    async def health_check(self) -> dict:
        """健康检查 — P7 实现。"""
        return {"status": "stub", "version": __import__("tradingagents.agent_harness").__version__}

    def __repr__(self) -> str:
        return f"<Harness data_dir={self.config.data_dir} llm_provider={self.config.llm_provider}>"


__all__ = ["Harness"]
