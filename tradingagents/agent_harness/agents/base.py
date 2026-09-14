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
    AgentRegistry (not via Plugin.agents() — that just returns refs)."""

    name: str
    description: str
    tools: list
    system_prompt: str = ""
    timeout_seconds: float = 60.0

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
