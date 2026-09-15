"""LLMFactory — create_llm(provider_name, model_name) → LLMProvider (v3 spec §6 init order step 4)."""
from __future__ import annotations

import logging
import os
from typing import Any

from .openai_provider import OpenAICompatibleProvider

LOGGER = logging.getLogger(__name__)


class LLMFactory:
    """Build :class:`LLMProvider` instances from (provider, model) pairs."""

    def __init__(
        self,
        default_provider: str | None = None,
        default_model: str | None = None,
        cache: "LLMResponseCache | None" = None,
    ) -> None:
        self.default_provider = default_provider or os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "")
        self.default_model = default_model or os.environ.get("TRADINGAGENTS_LLM_MODEL", "")
        self.cache = cache

    def make(
        self,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> OpenAICompatibleProvider:
        """Return a configured :class:`LLMProvider`.

        Falls back to factory defaults; raises ``ValueError`` if no
        provider/model can be resolved.
        """
        p = provider or self.default_provider
        m = model or self.default_model
        if not p or not m:
            raise ValueError(
                "LLM provider/model not configured; set TRADINGAGENTS_LLM_PROVIDER + "
                "TRADINGAGENTS_LLM_MODEL env vars, or pass explicitly"
            )
        return OpenAICompatibleProvider(p, m, base_url=base_url, cache=self.cache, **kwargs)

    def is_configured(self) -> bool:
        return bool(self.default_provider and self.default_model)
