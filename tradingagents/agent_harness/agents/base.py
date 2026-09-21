"""BaseAgent ABC + V1/V2 envelopes + governed proxies.

V1 envelope (existing): AgentInput / AgentResult / AgentContext.
V2 envelope (Task 13): AgentTask / AgentExecutionContext / AgentReply.

LegacyAgentAdapter bridges V1 BaseAgent.run() ↔ V2 AgentRuntime contract.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field


# ════════════════════════════════════════════════════════
# V1 envelopes (unchanged, kept for backwards compatibility)
# ════════════════════════════════════════════════════════


class AgentInput(BaseModel):
    """All V1 sub-agents receive the same envelope."""

    user_message: str
    context: dict = Field(default_factory=dict)
    plan_step: dict | None = None


class AgentResult(BaseModel):
    """All V1 sub-agents return this envelope."""

    success: bool
    content: str = ""
    structured_data: dict | None = None
    tool_calls: list[dict] = Field(default_factory=list)
    tool_results: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class AgentContext:
    """Per-turn agent context (session + tier + trace)."""

    def __init__(
        self,
        session_id: str,
        tier: int | None = None,
        trace_id: str | None = None,
        extra: dict | None = None,
    ) -> None:
        self.session_id = session_id
        self.tier = tier
        self.trace_id = trace_id
        self.extra = extra or {}


# ════════════════════════════════════════════════════════
# V2 descriptors and envelopes
# ════════════════════════════════════════════════════════


@dataclass(frozen=True)
class AgentDescriptor:
    """V2 agent descriptor — name + version + capabilities + scope + priority.

    Used by AgentRegistry V2 to support capability lookup and deterministic
    handoff.  ``version`` is the contract version (V1=1, V2=2, etc);
    unsupported versions are recorded as health errors but don't break
    Harness startup.
    """

    name: str
    version: int = 2
    capabilities: tuple[str, ...] = ()
    scope: str = "user"
    priority: int = 0


@dataclass(frozen=True)
class AgentReply:
    """V2 agent reply envelope."""

    success: bool
    content: str = ""
    structured_data: dict | None = None
    evidence: tuple[Any, ...] = ()
    confidence: float | None = None
    missing_items: tuple[str, ...] = ()
    outgoing: tuple[Any, ...] = ()
    errors: tuple[Any, ...] = ()


# ════════════════════════════════════════════════════════
# V2 Agent base (optional override path; existing V1 agents still work)
# ════════════════════════════════════════════════════════


class BaseAgent(ABC):
    """V1 BaseAgent ABC.

    Existing concrete agents (data / news / alpha / verifier / etc) inherit
    this and implement ``run(AgentInput, *, context=AgentContext) -> AgentResult``.

    For V2 dispatch, :class:`LegacyAgentAdapter` wraps V1 agents and
    translates the V2 envelope into V1 input/output.
    """

    name: str
    description: str
    tools: list
    system_prompt: str = ""
    timeout_seconds: float = 60.0

    def __init__(
        self,
        *,
        llm_factory: Any | None = None,
        tool_registry: Any | None = None,
        scope: Any | None = None,
    ) -> None:
        self.llm_factory = llm_factory
        self.tool_registry = tool_registry
        self.scope = scope

    def _llm_available(self) -> bool:
        return (
            self.llm_factory is not None
            and hasattr(self.llm_factory, "is_configured")
            and self.llm_factory.is_configured()
        )

    def _llm_complete(self, prompt: str, *, temperature: float = 0.0) -> str | None:
        if not self._llm_available():
            return None
        from tradingagents.agent_harness.core.token_usage import track_agent
        try:
            with track_agent(getattr(self, "name", type(self).__name__)):
                provider = self.llm_factory.make()
                response = provider.complete_text(
                    prompt=prompt, system=self.system_prompt, temperature=temperature
                )
            return getattr(response, "content", response) or None
        except Exception:
            return None

    @abstractmethod
    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        ...

    async def stream(
        self,
        input: AgentInput,
        *,
        context: AgentContext,
    ) -> AsyncIterator[tuple[str, dict]]:
        result = await self.run(input, context=context)
        yield ("agent_final", result.model_dump())

    def get_plan_steps(self) -> list[dict]:
        return [{"agent": self.name, "capability": self.description}]


# ════════════════════════════════════════════════════════
# LegacyAgentAdapter — V1 BaseAgent → V2 dispatch surface
# ════════════════════════════════════════════════════════


class LegacyAgentAdapter:
    """Adapt a V1 BaseAgent to the V2 dispatch contract.

    The runtime dispatcher calls ``adapter.run(v2_task, context=v2_ctx)``;
    this adapter:
    1. Translates ``v2_task`` (dict) → V1 ``AgentInput`` + ``AgentContext``
    2. Calls ``v1_agent.run(input, context=context)``
    3. Translates V1 ``AgentResult`` → V2 ``AgentReply``
    """

    def __init__(
        self,
        *,
        v1_agent: BaseAgent,
        descriptor: AgentDescriptor,
    ) -> None:
        self.v1_agent = v1_agent
        self.descriptor = descriptor
        self.name = descriptor.name

    async def run(
        self,
        v2_task: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> AgentReply:
        # Translate V2 → V1
        v1_input = AgentInput(
            user_message=v2_task.get("objective", ""),
            context={
                "inputs": v2_task.get("inputs", {}),
                "run_id": v2_task.get("run_id"),
                "task_id": v2_task.get("task_id"),
                "dependency_results": v2_task.get("dependency_results", []),
            },
            plan_step=v2_task.get("plan_step"),
        )
        v1_ctx = AgentContext(
            session_id=str(context.get("session_id", "")),
            tier=context.get("tier"),
            trace_id=context.get("trace_id"),
            extra=context,
        )
        v1_result = await self.v1_agent.run(v1_input, context=v1_ctx)
        # Translate V1 → V2
        return AgentReply(
            success=bool(v1_result.success),
            content=v1_result.content,
            structured_data=v1_result.structured_data,
            evidence=tuple(v1_result.tool_results or []),
            confidence=None,
            missing_items=tuple(),
            outgoing=tuple(),
            errors=tuple(v1_result.errors or []),
        )


# ════════════════════════════════════════════════════════
# Governed proxies — V1 tool/llm access through V2 executors
# ════════════════════════════════════════════════════════


class GovernedToolProxy:
    """Wrap a V2 ToolExecutor and expose a V1-style ``invoke`` for V1 agents.

    V1 agents that call ``tool_registry.invoke(name, args)`` are routed
    through this proxy → V2 ToolExecutor.invoke(governed + approval).
    """

    def __init__(self, *, tool_executor: Any) -> None:
        self.tool_executor = tool_executor

    def invoke(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        on_approval: Any | None = None,
    ) -> Any:
        return self.tool_executor.invoke(
            tool_name, args, on_approval=on_approval
        )


class GovernedLLMProxy:
    """Wrap a V2 LLMExecutor for V1 agents that hold a ``llm_factory``.

    Used by ``LegacyAgentAdapter`` when the V1 agent's ``_llm_complete``
    path needs to go through the governed executor (for token budgeting).
    """

    def __init__(self, *, llm_executor: Any) -> None:
        self.llm_executor = llm_executor

    def complete_text(
        self,
        *,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> Any:
        return self.llm_executor.complete_text(
            prompt=prompt, system=system, temperature=temperature
        )


__all__ = [
    "AgentContext",
    "AgentDescriptor",
    "AgentInput",
    "AgentReply",
    "AgentResult",
    "BaseAgent",
    "GovernedLLMProxy",
    "GovernedToolProxy",
    "LegacyAgentAdapter",
]
