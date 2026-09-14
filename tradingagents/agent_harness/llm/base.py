"""LLMProvider ABC — harness-side unified LLM interface (v3 spec §3 llm/).

This sits on top of ``tradingagents.llm_clients`` (which has 5 native
provider implementations) and exposes a single ``complete()`` method
that all harness components (orchestrator + 6 sub-agents + L3 judge)
can call.

Adding a new provider = register a factory in ``LLM_REGISTRY``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """OpenAI-compatible chat message."""

    role: str = Field(..., description="system | user | assistant | tool")
    content: str = ""


class LLMResponse(BaseModel):
    """Unified response envelope so callers don't depend on provider shapes."""

    content: str
    provider: str
    model: str
    usage: dict[str, int] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class LLMProvider(ABC):
    """Harness-side unified LLM provider.

    Implementations MUST override :meth:`complete` and :attr:`name`.
    """

    name: str = ""

    @abstractmethod
    def complete(
        self,
        messages: Iterable[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        """Send messages and return a single assistant response."""

    def complete_text(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        """Convenience: single user prompt (+ optional system) → string response."""
        msgs: list[ChatMessage] = []
        if system:
            msgs.append(ChatMessage(role="system", content=system))
        msgs.append(ChatMessage(role="user", content=prompt))
        return self.complete(msgs, temperature=temperature).content
