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
from .failure import classify_llm_error
from .cache import LLMResponseCache, make_cache_key
from tradingagents.agent_harness.core.token_usage import (
    get_active_agent,
    get_active_store,
    get_active_surface,
)

LOGGER = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    """Wrap an OpenAI-protocol client as an :class:`LLMProvider`."""

    def __init__(
        self,
        provider: str,
        model: str,
        base_url: str | None = None,
        cache: "LLMResponseCache | None" = None,
        **kwargs,
    ) -> None:
        self._provider_name = provider
        self._model = model
        self._client = create_llm_client(provider, model, base_url=base_url, **kwargs)
        # Optional response cache (P2-LLM cache): same prompt → same response
        self._cache = cache

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
        # LLM response cache (P2): short-circuit identical requests
        if self._cache is not None:
            key = make_cache_key(
                messages, system=None,
                temperature=temperature, max_tokens=max_tokens, model=self._model,
            )
            cached = self._cache.get(key)
            if cached is not None:
                # Mark this as a cache hit so token accounting sees zero real tokens
                cached_copy = cached.model_copy()
                cached_copy.usage = {
                    **(cached.usage or {}),
                    "cached": True,
                }
                return cached_copy
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
            # Re-raise as a normalized LlmFailure so callers (orchestrator,
            # agents, L3 judge, frontend) get a structured kind field
            # instead of having to parse strings.
            LOGGER.warning("LLM call failed: %s", e)
            failure = classify_llm_error(
                e, provider=self._provider_name, model=self._model,
            )
            # Bump errors counter (zero tokens — we never saw the response).
            store = get_active_store()
            if store is not None:
                store.record(
                    get_active_agent(),
                    input_tokens=0, output_tokens=0, total_tokens=0,
                    surface=get_active_surface(), errored=True,
                )
            raise failure from e

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
        usage = {
            "input_tokens": int(token_usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(token_usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(token_usage.get("total_tokens", 0) or 0),
        }
        # Record into active TokenUsageStore (if attached by caller).
        # Sub-agents wrap their LLM call with track_agent(name) so the
        # usage ends up bucketed by agent name; surface partitioning
        # hooks into P0-4.
        store = get_active_store()
        if store is not None:
            store.record(
                get_active_agent(),
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                total_tokens=usage["total_tokens"] or None,
                surface=get_active_surface(),
            )
        response = LLMResponse(
            content=content or "",
            provider=self._provider_name,
            model=self._model,
            usage=usage,
        )
        if self._cache is not None:
            try:
                self._cache.put(key, response)
            except Exception:
                LOGGER.debug("cache store failed", exc_info=True)
        return response


# Registry helper — let callers pre-register this provider for any name.
def make_openai_compatible(provider: str, model: str, **kwargs) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(provider, model, **kwargs)
