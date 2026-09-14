"""Harness 主类 — 组装所有组件(v3 spec §6, N4/N15/N36 fix).

Init order (must follow §6.0 docstring):
1.  config (HarnessConfig)
2.  tool_registry (ToolRegistry) + builtin tools (P3)
3.  agent_registry (AgentRegistry) + 6 builtin agents (P5)
4.  llm_factory (LLMFactory)
5.  data_registry (PROVIDERS dict — P2)
6.  memory (MemoryManager)
7.  audit (AuditLogger)
8.  health (HealthChecker — P7)
9.  context_priority (ContextPriority — P4)
10. retry_policy + circuit_breaker (P4)
11. routing (fast_route — P4)
12. plugin_registry (PluginRegistry + entry_points — P6)
13. orchestrator (Orchestrator, depends on 2-11)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncIterator

from tradingagents.agent_harness.config.schema import HarnessConfig
from tradingagents.agent_harness.core import (
    CircuitBreaker,
    ContextPriority,
    Orchestrator,
    RetryPolicy,
)
from tradingagents.agent_harness.tools import (
    ToolRegistry,
    install_builtin_tools,
)

LOGGER = logging.getLogger(__name__)


class Harness:
    """Harness 主类 — 组装 tool/agent/registry + orchestrator, 提供统一入口."""

    def __init__(self, config: HarnessConfig | None = None) -> None:
        self.config = config or HarnessConfig.from_env()

        # 2. ToolRegistry + builtin tools
        self.tool_registry = ToolRegistry()
        install_builtin_tools(self.tool_registry)

        # 3. AgentRegistry (P5 阶段填充 6 个 agent)
        # 延迟导入避免 P4 阶段循环
        from tradingagents.agent_harness.agents.registry import AgentRegistry
        self.agent_registry = AgentRegistry()

        # 5. data_registry (PROVIDERS dict)
        from tradingagents.data.providers.registry import PROVIDERS
        self.data_registry = PROVIDERS

        # 6. memory — placeholder (P5+ 实现)
        self.memory = None

        # 7. audit — placeholder (P5+ 实现)
        self.audit = None

        # 8. health — placeholder (P7 实现)
        self.health = None

        # 9. context priority
        self.context_priority = ContextPriority()

        # 10. retry + circuit breaker
        self.retry_policy = RetryPolicy(max_retries=2, backoff_seconds=0.5, exponential=True)
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, reset_seconds=30.0)

        # 12. plugin registry — placeholder (P6)
        self.plugin_registry = None

        # 4. llm_factory — placeholder (依赖 llm_clients)
        self.llm_factory = None

        # 13. orchestrator (depends on 2, 3, 9, 10)
        self.orchestrator = Orchestrator(
            tool_registry=self.tool_registry,
            agent_registry=self.agent_registry,
            llm_factory=self.llm_factory,
            context_priority=self.context_priority,
            retry_policy=self.retry_policy,
            circuit_breaker=self.circuit_breaker,
            audit=self.audit,
        )

        LOGGER.info(
            "Harness ready (tools=%d, providers=%d, stage=P3+P4)",
            len(self.tool_registry.list_all()),
            len(self.data_registry),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def stream_chat(
        self,
        session_id: str,
        user_message: str,
        *,
        history: list | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Tier-aware unified entry — delegates to orchestrator."""
        async for event in self.orchestrator.stream_chat(
            session_id=session_id,
            user_message=user_message,
            history=history,
        ):
            yield event

    def get_tool(self, name: str):
        return self.tool_registry.get(name)

    def list_tools(self):
        return self.tool_registry.list_all()
