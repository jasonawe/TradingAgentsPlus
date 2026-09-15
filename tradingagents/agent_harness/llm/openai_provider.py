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

from .app_identity import AppIdentity, default_app_identity
from .base import ChatMessage, LLMProvider, LLMResponse
from .failure import classify_llm_error
from .cache import LLMResponseCache, make_cache_key
from tradingagents.agent_harness.core.retry import (
    ResolvedRetryPolicy,
    retry_resolved_sync,
)
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
        retry_policy: ResolvedRetryPolicy | None = None,
        **kwargs,
    ) -> None:
        self._provider_name = provider
        self._model = model
        # W3-D6 R8: AppIdentity User-Agent 强制
        # 自动注入 identity headers 到 ChatOpenAI 的 default_headers,
        # provider 侧日志能按我们 app + 版本归因流量。
        # 调用方可以在 kwargs 里显式传 default_headers 完全覆盖;
        # 也可以传 identity=AppIdentity(...) 自定义身份。
        identity: AppIdentity = kwargs.pop("identity", None) or default_app_identity()
        if "default_headers" not in kwargs:
            kwargs["default_headers"] = identity.headers()
        self._identity = identity
        self._client = create_llm_client(provider, model, base_url=base_url, **kwargs)
        # Optional response cache (P2-LLM cache): same prompt → same response
        self._cache = cache
        # spec R7 ResolvedRetryPolicy retryableCodes — kind-based 重试决策。
        # 默认 = TRANSIENT_KINDS(RATE_LIMIT / TIMEOUT / NETWORK / SERVER)。
        # CONTEXT_TOO_LONG / AUTH / NOT_FOUND / INVALID_OUTPUT 默认不重试。
        self._retry_policy = retry_policy or ResolvedRetryPolicy()

    def set_retry_policy(self, policy: ResolvedRetryPolicy) -> None:
        """Wire or replace the retry policy post-init (matches the
        ``set_checkpoint_store`` / ``set_session_store`` pattern)."""
        self._retry_policy = policy

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def identity(self) -> AppIdentity:
        # W3-D6 R8: expose the AppIdentity this provider was constructed with
        return self._identity

    def complete(
        self,
        messages: Iterable[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
    ) -> LLMResponse:
        """Send messages; spec R7 — wraps with ResolvedRetryPolicy retry.

        Cache short-circuit stays outside retry (cache hit ≠ retryable failure).
        """
        # LLM response cache (P2): short-circuit identical requests
        # Cache 命中不走 retry — 不算可重试失败。
        if self._cache is not None:
            key = make_cache_key(
                messages, system=None,
                temperature=temperature, max_tokens=max_tokens, model=self._model,
            )
            cached = self._cache.get(key)
            if cached is not None:
                cached_copy = cached.model_copy()
                cached_copy.usage = {
                    **(cached.usage or {}),
                    "cached": True,
                }
                return cached_copy
        # spec R7:用 ResolvedRetryPolicy 决策哪些 LlmFailure 重试。
        # kind 不在 retryable_codes 里(或非 LlmFailure) → 立即 raise。
        # The inner callable raises LlmFailure; retry_resolved_sync decides.
        return retry_resolved_sync(
            lambda: self._do_complete(
                messages, temperature=temperature,
                max_tokens=max_tokens, stop=stop, cache_key=(
                    make_cache_key(
                        messages, system=None,
                        temperature=temperature, max_tokens=max_tokens,
                        model=self._model,
                    ) if self._cache is not None else None
                ),
            ),
            policy=self._retry_policy,
        )

    def _do_complete(
        self,
        messages: Iterable[ChatMessage],
        *,
        temperature: float,
        max_tokens: int | None,
        stop: list[str] | None,
        cache_key: str | None,
    ) -> LLMResponse:
        """Inner LLM call — raises :class:`LlmFailure` on failure."""
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
        # spec R5: disjoint 6 字段。
        # OpenAI / Anthropic / Gemini 共用的 nested 子对象:
        #   prompt_tokens_details.cached_tokens           → cache_read
        #   cache_creation_input_tokens (Anthropic)       → cache_write
        #   completion_tokens_details.reasoning_tokens    → reasoning
        # 同时支持 Anthropic 顶层字段:
        #   cache_read_input_tokens / cache_creation_input_tokens
        prompt_details = token_usage.get("prompt_tokens_details", {}) or {}
        completion_details = token_usage.get("completion_tokens_details", {}) or {}
        cache_read = int(
            prompt_details.get("cached_tokens", 0)
            or token_usage.get("cache_read_input_tokens", 0)
            or 0
        )
        cache_write = int(
            token_usage.get("cache_creation_input_tokens", 0) or 0
        )
        reasoning = int(
            completion_details.get("reasoning_tokens", 0) or 0
        )
        # spec R5:6 字段。兼容 OpenAI / Anthropic 字段约定:
        #   OpenAI:   prompt_tokens / completion_tokens
        #   Anthropic: input_tokens  / output_tokens
        input_t = int(
            token_usage.get("prompt_tokens", 0)
            or token_usage.get("input_tokens", 0)
            or 0
        )
        output_t = int(
            token_usage.get("completion_tokens", 0)
            or token_usage.get("output_tokens", 0)
            or 0
        )
        usage = {
            "input_tokens": input_t,
            "output_tokens": output_t,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "reasoning_tokens": reasoning,
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
                cache_read_tokens=usage["cache_read_tokens"],
                cache_write_tokens=usage["cache_write_tokens"],
                reasoning_tokens=usage["reasoning_tokens"],
                total_tokens=usage["total_tokens"] or None,
                surface=get_active_surface(),
            )
        response = LLMResponse(
            content=content or "",
            provider=self._provider_name,
            model=self._model,
            usage=usage,
        )
        if self._cache is not None and cache_key is not None:
            try:
                self._cache.put(cache_key, response)
            except Exception:
                LOGGER.debug("cache store failed", exc_info=True)
        return response


# Registry helper — let callers pre-register this provider for any name.
def make_openai_compatible(provider: str, model: str, **kwargs) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(provider, model, **kwargs)
