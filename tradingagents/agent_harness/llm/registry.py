"""LLM_REGISTRY — provider name → factory callable (v3 spec §3 llm/).

Default registry exposes ``openai`` → :func:`make_openai_compatible`.
Callers can register additional providers at runtime via :func:`register`.
"""
from __future__ import annotations

from typing import Callable

from .openai_provider import OpenAICompatibleProvider, make_openai_compatible

# §P3-4 — every OpenAI-compatible endpoint listed in
# tradingagents/llm_clients/model_catalog.MODEL_OPTIONS is registered
# here, so the settings-page PATCH endpoint accepts any catalog
# provider instead of rejecting "unknown provider" for entries that
# the UI happily advertises (qwen / xai / glm / deepseek / ...).
# Each entry maps to the same ``make_openai_compatible`` factory —
# provider-specific URL / API key are picked up from
# ``OpenAICompatibleProvider``'s own base-URL env lookup.
LLM_REGISTRY: dict[str, Callable[..., OpenAICompatibleProvider]] = {
    # v3-spec baseline providers
    "openai": make_openai_compatible,
    "anthropic": make_openai_compatible,
    "google": make_openai_compatible,
    "azure": make_openai_compatible,
    "bedrock": make_openai_compatible,
    "minimax": make_openai_compatible,
    "minimax-cn": make_openai_compatible,
    "minimax_cn": make_openai_compatible,
    "ollama": make_openai_compatible,
    "vllm": make_openai_compatible,
    # Catalog-aligned providers (Phase 2)
    "xai": make_openai_compatible,
    "deepseek": make_openai_compatible,
    "qwen": make_openai_compatible,
    "qwen-cn": make_openai_compatible,
    "glm": make_openai_compatible,
    "glm-cn": make_openai_compatible,
    "groq": make_openai_compatible,
    "kimi": make_openai_compatible,
    "mistral": make_openai_compatible,
    "nvidia": make_openai_compatible,
    "openai_compatible": make_openai_compatible,
}


def register(name: str, factory: Callable[..., OpenAICompatibleProvider]) -> None:
    """Register a custom provider factory under ``name``."""
    if name in LLM_REGISTRY:
        raise ValueError(f"provider {name!r} already registered")
    LLM_REGISTRY[name] = factory


_default_provider: str = "openai"


def get_default_provider_name() -> str:
    return _default_provider


def set_default_provider(name: str) -> None:
    if name not in LLM_REGISTRY:
        raise KeyError(f"unknown provider {name!r}; known: {sorted(LLM_REGISTRY)}")
    global _default_provider
    _default_provider = name
