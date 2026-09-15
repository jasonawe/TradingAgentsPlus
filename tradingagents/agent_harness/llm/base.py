"""LLMProvider ABC — harness-side unified LLM interface (v3 spec §3 llm/).

This sits on top of ``tradingagents.llm_clients`` (which has 5 native
provider implementations) and exposes a single ``complete()`` method
that all harness components (orchestrator + 6 sub-agents + L3 judge)
can call.

Adding a new provider = register a factory in ``LLM_REGISTRY``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

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


# W3-D6 R4 — streaming chunk protocol. 7 kinds mirror dsh's
# StreamChunk union; a non-streaming provider can use the
# ``complete_then_yield`` fallback in the abstract base.
STREAM_KINDS = (
    "text_delta",   # incremental text  (content)
    "tool_call",    # tool call         (name, args)
    "tool_result",  # tool result       (name, result)
    "finish",       # generation done   (reason)
    "error",        # error             (error, code)
    "usage",        # token usage       (usage)
    "block",        # block-level update (blocks)
)


@dataclass
class StreamChunk:
    """One unit of a streaming LLM response.

    The ``kind`` field is one of :data:`STREAM_KINDS`; the other
    fields are populated per-kind (see :data:`STREAM_KINDS` for
    which field carries the payload). Callers should branch on
    ``kind`` and ignore unused fields.
    """

    kind: str
    content: str = ""
    name: str = ""
    args: dict[str, Any] | None = None
    result: Any = None
    reason: str = ""
    error: str = ""
    code: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    blocks: list[Any] = field(default_factory=list)
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "content": self.content,
            "name": self.name,
            "args": self.args,
            "result": self.result,
            "reason": self.reason,
            "error": self.error,
            "code": self.code,
            "usage": self.usage,
            "blocks": self.blocks,
            "ts": self.ts,
        }


class LLMProvider(ABC):
    """Harness-side unified LLM provider.

    Implementations MUST override :meth:`complete` and :attr:`name`;
    :meth:`stream` has a ``complete_then_yield`` fallback for
    non-streaming providers (one ``text_delta`` + ``finish``).
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

    def stream(
        self,
        messages: Iterable[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> Iterable[StreamChunk]:
        """Yield :class:`StreamChunk` events.

        Default fallback: call :meth:`complete` and emit one
        ``text_delta`` + one ``finish`` chunk. Streaming providers
        override this to emit incremental chunks.
        """
        import time as _time
        resp = self.complete(
            list(messages), temperature=temperature,
            max_tokens=max_tokens, stop=stop,
        )
        yield StreamChunk(kind="text_delta", content=resp.content, ts=_time.time())
        if resp.usage:
            yield StreamChunk(kind="usage", usage=resp.usage, ts=_time.time())
        yield StreamChunk(kind="finish", reason="stop", ts=_time.time())

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

    def stream_text(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.0,
    ) -> Iterable[StreamChunk]:
        """Convenience: single user prompt (+ optional system) → chunk stream."""
        msgs: list[ChatMessage] = []
        if system:
            msgs.append(ChatMessage(role="system", content=system))
        msgs.append(ChatMessage(role="user", content=prompt))
        return self.stream(msgs, temperature=temperature)
