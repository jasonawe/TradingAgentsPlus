"""OpenAICompatibleProvider — wraps ``tradingagents.llm_clients.BaseLLMClient``.

Used by every provider that speaks the OpenAI Chat Completions protocol:
- OpenAI (native)
- Anthropic (via OpenAI-compat adapter)
- MiniMax / MiniMax-CN
- Google (via OpenAI-compat)
- Ollama (local)
- vLLM / LM Studio (local OpenAI-compat)

Native providers (Anthropic / Google / Azure / Bedrock) use the same
interface because the underlying ``tradingagents.llm_clients`` abstracts
both shapes — see ``tradingagents.llm_clients/factory.py``.
"""
from __future__ import annotations

import logging
from typing import Iterable

from tradingagents.llm_clients.factory import create_llm_client

from .base import ChatMessage, LLMProvider, LLMResponse

LOGGER = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    """Wrap an OpenAI-protocol client as an :class:`LLMProvider`."""

    def __init__(
        self,
        provider: str,
        model: str,
        base_url: str | None = None,
        **kwargs,
    ) -> None:
        self._provider_name = provider
        self._model = model
        self._client = create_llm_client(provider, model, base_url=base_url, **kwargs)

    @property
    def name(self) -> str:
        return self._provider_name

    def complete(
        self,
        messages: Iterable[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        llm = self._client.get_llm()
        langchain_msgs = [{"role": m.role, "content": m.content} for m in messages]
        payload: list = []
        if langchain_msgs and langchain_msgs[0]["role"] == "system":
            payload.append(("system", langchain_msgs[0]["content"]))
            user_msgs = langchain_msgs[1:]
        else:
            user_msgs = langchain_msgs
        for m in user_msgs:
            payload.append((m["role"], m["content"]))

        kwargs: dict = {"temperature": temperature}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if stop:
            kwargs["stop"] = stop

        try:
            response = llm.invoke(payload, **kwargs)
        except Exception as e:
            LOGGER.warning("LLM call failed: %s", e)
            raise

        content = getattr(response, "content", str(response))
        if isinstance(content, list):
            # Normalize blocks (OpenAI Responses / Gemini 3 sometimes return lists)
            content = "\n".join(
                item.get("text", "") if isinstance(item, dict) and item.get("type") == "text"
                else item if isinstance(item, str) else ""
                for item in content
            )

        usage_meta = getattr(response, "response_metadata", {}) or {}
        token_usage = usage_meta.get("token_usage", {}) or {}
        return LLMResponse(
            content=content or "",
            provider=self._provider_name,
            model=self._model,
            usage={
                "input_tokens": int(token_usage.get("prompt_tokens", 0) or 0),
                "output_tokens": int(token_usage.get("completion_tokens", 0) or 0),
                "total_tokens": int(token_usage.get("total_tokens", 0) or 0),
            },
        )


# Registry helper — let callers pre-register this provider for any name.
def make_openai_compatible(provider: str, model: str, **kwargs) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(provider, model, **kwargs)
