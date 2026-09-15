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

        # 4. llm_factory — wraps tradingagents/llm_clients (v3 spec §3 llm/).
        # Built BEFORE agents so we can inject it into them.
        from tradingagents.agent_harness.llm import LLMFactory
        self.llm_factory = LLMFactory(
            default_provider=self.config.llm_provider,
            default_model=self.config.llm_model,
        )

        # 4b. judge_factory — L3 LLM-judge 模型 (spec §D6 N89 fix: judge
        # 不复用主 LLM)。judge_provider/model 为空时回退到主 LLM factory。
        if self.config.judge_provider and self.config.judge_model:
            self.judge_factory = LLMFactory(
                default_provider=self.config.judge_provider,
                default_model=self.config.judge_model,
            )
        else:
            self.judge_factory = self.llm_factory

        # 3. AgentRegistry + 6 builtin agents (N64 fix, P8 LLM wiring).
        # Inject llm_factory + tool_registry so each agent.run() can do real
        # work; without injection the agents fall back to heuristic stubs.
        from tradingagents.agent_harness.agents import (
            AgentRegistry,
            AlphaAgent,
            DataAgent,
            NewsAgent,
            PlannerAgent,
            SynthesizerAgent,
            VerifierAgent,
        )
        self.agent_registry = AgentRegistry()
        agent_classes = (
            PlannerAgent,
            VerifierAgent,
            DataAgent,
            AlphaAgent,
            NewsAgent,
            SynthesizerAgent,
        )
        for cls in agent_classes:
            if cls is VerifierAgent:
                # VerifierAgent 拿额外的 judge_factory + enable_l3
                agent = cls(
                    llm_factory=self.llm_factory,
                    tool_registry=self.tool_registry,
                    judge_factory=self.judge_factory,
                    enable_l3=self.config.enable_l3,
                )
            else:
                agent = cls(
                    llm_factory=self.llm_factory,
                    tool_registry=self.tool_registry,
                )
            self.agent_registry.register(agent)

        # 5. data_registry (PROVIDERS dict)
        from tradingagents.data.providers.registry import PROVIDERS
        self.data_registry = PROVIDERS

        # 6. memory — L1/L2/L3 facade (v3 spec §3 memory/)
        from tradingagents.agent_harness.memory import MemoryManager
        self.memory = MemoryManager(data_dir=str(self.config.data_dir))

        # 7. audit (P7)
        from tradingagents.agent_harness.observability import AuditLogger
        self.audit = AuditLogger(self.config.data_dir)

        # 8. health (P7)
        from tradingagents.agent_harness.observability import (
            HealthChecker, Metrics, Tracer,
        )
        self.health = HealthChecker()
        self.metrics = Metrics()
        self.tracer = Tracer()

        # 9. context priority — auto-injects memory L1/L2 (P8 Task 2)
        self.context_priority = ContextPriority(memory=self.memory)

        # 10. retry + circuit breaker
        self.retry_policy = RetryPolicy(max_retries=2, backoff_seconds=0.5, exponential=True)
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, reset_seconds=30.0)

        # 12. plugin registry + builtin plugins (P6)
        from tradingagents.agent_harness.plugins import PluginRegistry
        from tradingagents.agent_harness.plugins.builtin import (
            AlertPlugin, NewsPlugin, QuantPlugin,
        )
        self.plugin_registry = PluginRegistry(self)
        for cls in (QuantPlugin, NewsPlugin, AlertPlugin):
            self.plugin_registry.register(cls())
        self.plugin_registry.discover_entry_points()

        # 13. orchestrator (depends on 2, 3, 9, 10)
        self.orchestrator = Orchestrator(
            tool_registry=self.tool_registry,
            agent_registry=self.agent_registry,
            llm_factory=self.llm_factory,
            context_priority=self.context_priority,
            retry_policy=self.retry_policy,
            circuit_breaker=self.circuit_breaker,
            audit=self.audit,
            enable_l3=self.config.enable_l3,
            judge_factory=self.judge_factory,
        )

        LOGGER.info(
            "Harness ready (tools=%d, providers=%d, stage=P3+P4+P5+P6+P7)",
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

    def set_checkpoint_store(self, store) -> None:
        """Wire a ``HarnessCheckpointStore`` into the underlying orchestrator.

        After this call, every ``stream_chat`` invocation will persist
        per-session checkpoints so a crashed session can be resumed via
        ``resume(session_id)``.
        """
        self.orchestrator._checkpoint_store = store

    async def resume(
        self, session_id: str,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Resume a crashed/interrupted session from its last checkpoint.

        See ``Orchestrator.resume`` for details.
        """
        async for event in self.orchestrator.resume(session_id):
            yield event

    def get_tool(self, name: str):
        return self.tool_registry.get(name)

    def list_tools(self):
        return self.tool_registry.list_all()


# ---------------------------------------------------------------------------
# FastAPI integration helper
# ---------------------------------------------------------------------------

def mount_health_endpoint(app, harness: "Harness", path: str = "/api/harness/health") -> None:
    """Attach the `` /api/harness/health `` endpoint to ``app``.

    Caller decides when to call this (e.g. inside ``web/app.create_app()``);
    we keep the harness package free-free of FastAPI dependencies.
    """
    try:
        from fastapi import HTTPException  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "FastAPI is required to mount the health endpoint; "
            "pip install fastapi"
        ) from e

    @app.get(path)
    async def _health():  # type: ignore[misc]
        try:
            return await harness.health.check_all(harness)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    LOGGER.info("mounted harness health endpoint at %s", path)
