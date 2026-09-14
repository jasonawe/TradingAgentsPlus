"""LLM_REGISTRY — provider name → factory callable (v3 spec §3 llm/).

Default registry exposes ``openai`` → :func:`make_openai_compatible`.
Callers can register additional providers at runtime via :func:`register`.
"""
from __future__ import annotations

from typing import Callable

from .openai_provider import OpenAICompatibleProvider, make_openai_compatible

LLM_REGISTRY: dict[str, Callable[..., OpenAICompatibleProvider]] = {
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
