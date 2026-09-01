import os
from typing import Any

from langchain_openai import AzureChatOpenAI

from .base_client import (
    BaseLLMClient,
    configure_deadline_policy,
    invoke_with_deadline,
    normalize_content,
)

_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "api_key", "reasoning_effort", "temperature",
    "callbacks", "http_client", "http_async_client",
)


class NormalizedAzureChatOpenAI(AzureChatOpenAI):
    """AzureChatOpenAI with normalized content output."""

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(
            invoke_with_deadline(self, super().invoke, input, config, **kwargs)
        )


class AzureOpenAIClient(BaseLLMClient):
    """Client for Azure OpenAI deployments.

    Requires environment variables:
        AZURE_OPENAI_API_KEY: API key
        AZURE_OPENAI_ENDPOINT: Endpoint URL (e.g. https://<resource>.openai.azure.com/)
        AZURE_OPENAI_DEPLOYMENT_NAME: Deployment name
        OPENAI_API_VERSION: API version (e.g. 2025-03-01-preview)
    """

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """Return configured AzureChatOpenAI instance."""
        self.warn_if_unknown_model()

        llm_kwargs = {
            "model": self.model,
            "azure_deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", self.model),
        }

        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        llm = NormalizedAzureChatOpenAI(**llm_kwargs)
        return configure_deadline_policy(
            llm,
            deadline_supplier=self.kwargs.get("deadline_supplier"),
            timeout_cap=self.kwargs.get("timeout"),
            checkpoint=self.kwargs.get("external_request_checkpoint"),
        )

    def validate_model(self) -> bool:
        """Azure accepts any deployed model name."""
        return True
