"""LLM adapter layer — wraps ``tradingagents/llm_clients/`` for the harness.

Three layers in v3 spec §3:
- base.py — LLMProvider ABC + ChatMessage Pydantic model
- openai_provider.py — OpenAICompatibleProvider (OpenAI/Claude/MiniMax/Ollama all use this protocol)
- factory.py — LLMFactory: create_llm(provider_name, model_name) → LLMProvider
- registry.py — LLM_REGISTRY dict + get_default_provider()

LLM integration is optional: when ``llm_factory=None`` (default), the
orchestrator / agents fall back to heuristic placeholders. Setting
``HarnessConfig.llm_provider="openai"`` (etc.) wires real LLM calls.
"""
from .base import ChatMessage, LLMProvider, LLMResponse, StreamChunk, STREAM_KINDS
from .factory import LLMFactory
from .openai_provider import OpenAICompatibleProvider
from .registry import LLM_REGISTRY, get_default_provider_name, set_default_provider

__all__ = [
    "ChatMessage",
    "LLMProvider",
    "LLMResponse",
    "LLMFactory",
    "OpenAICompatibleProvider",
    "LLM_REGISTRY",
    "get_default_provider_name",
    "set_default_provider",
]
