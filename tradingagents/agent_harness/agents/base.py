"""BaseAgent ABC + AgentInput/AgentResult (v3 spec §4.2)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field


class AgentInput(BaseModel):
    """All sub-agents receive the same envelope."""

    user_message: str
    context: dict = Field(default_factory=dict)
    plan_step: dict | None = None


class AgentResult(BaseModel):
    """All sub-agents return this envelope."""

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


class BaseAgent(ABC):
    """Sub-agent contract — N64 fix: agents are registered directly on
    AgentRegistry (not via Plugin.agents() — that just returns refs).

    Optional LLM / tool injection (P8):
        Agents that take ``llm_factory`` + ``tool_registry`` will do real
        work when wired up by the Harness; otherwise they fall back to
        heuristic stubs so they remain unit-testable without API keys.
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
    ) -> None:
        self.llm_factory = llm_factory
        self.tool_registry = tool_registry

    # ------------------------------------------------------------------
    # LLM / tool helpers (shared by all 6 agents)
    # ------------------------------------------------------------------
    def _llm_available(self) -> bool:
        return (
            self.llm_factory is not None
            and hasattr(self.llm_factory, "is_configured")
            and self.llm_factory.is_configured()
        )

    def _llm_complete(self, prompt: str, *, temperature: float = 0.0) -> str | None:
        """Single-turn LLM call. Returns ``None`` on any failure (caller falls back).

        Wrapped in ``track_agent(self.name)`` so the token usage ends up
        bucketed under this agent's name in the active ``TokenUsageStore``.
        Subclasses just override ``name``; the bookkeeping is automatic.
        """
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
        """Default streaming — subclasses can override."""
        result = await self.run(input, context=context)
        yield ("agent_final", result.model_dump())

    def get_plan_steps(self) -> list[dict]:
        return [{"agent": self.name, "capability": self.description}]
