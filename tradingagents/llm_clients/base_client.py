import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any


def normalize_content(response):
    """Normalize LLM response content to a plain string.

    Multiple providers (OpenAI Responses API, Google Gemini 3) return content
    as a list of typed blocks, e.g. [{'type': 'reasoning', ...}, {'type': 'text', 'text': '...'}].
    Downstream agents expect response.content to be a string. This extracts
    and joins the text blocks, discarding reasoning/metadata blocks.
    """
    content = response.content
    if isinstance(content, list):
        texts = [
            item.get("text", "") if isinstance(item, dict) and item.get("type") == "text"
            else item if isinstance(item, str) else ""
            for item in content
        ]
        response.content = "\n".join(t for t in texts if t)
    return response


def resolve_invocation_timeout(instance: Any, explicit: Any = None) -> float | None:
    """Resolve the request timeout immediately before a provider invocation."""

    supplier = getattr(instance, "_deadline_supplier", None)
    if not callable(supplier):
        return explicit
    remaining = max(0.0, float(supplier()))
    cap = getattr(instance, "_request_timeout_cap", None)
    values = [remaining]
    if explicit is not None:
        values.append(max(0.0, float(explicit)))
    if cap is not None:
        values.append(max(0.0, float(cap)))
    return min(values)


def invoke_with_deadline(
    instance: Any,
    invoke: Callable[..., Any],
    input: Any,
    config: Any = None,
    **kwargs: Any,
) -> Any:
    """Run one provider call with Worker checkpoints and a fresh deadline cut."""

    checkpoint = getattr(instance, "_external_request_checkpoint", None)
    if callable(checkpoint):
        checkpoint()
    timeout = resolve_invocation_timeout(instance, kwargs.get("timeout"))
    if callable(getattr(instance, "_deadline_supplier", None)):
        kwargs["timeout"] = timeout
    try:
        return invoke(input, config, **kwargs)
    finally:
        if callable(checkpoint):
            checkpoint()


def configure_deadline_policy(
    llm: Any,
    *,
    deadline_supplier: Callable[[], float] | None,
    timeout_cap: float | None = None,
    checkpoint: Callable[[], Any] | None = None,
) -> Any:
    if deadline_supplier is None and checkpoint is None:
        return llm
    object.__setattr__(llm, "_deadline_supplier", deadline_supplier)
    object.__setattr__(llm, "_request_timeout_cap", timeout_cap)
    object.__setattr__(llm, "_external_request_checkpoint", checkpoint)
    return llm


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients."""

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        self.model = model
        self.base_url = base_url
        self.kwargs = kwargs

    def get_provider_name(self) -> str:
        """Return the provider name used in warning messages."""
        provider = getattr(self, "provider", None)
        if provider:
            return str(provider)
        return self.__class__.__name__.removesuffix("Client").lower()

    def warn_if_unknown_model(self) -> None:
        """Warn when the model is outside the known list for the provider."""
        if self.validate_model():
            return

        warnings.warn(
            (
                f"Model '{self.model}' is not in the known model list for "
                f"provider '{self.get_provider_name()}'. Continuing anyway."
            ),
            RuntimeWarning,
            stacklevel=2,
        )

    @abstractmethod
    def get_llm(self) -> Any:
        """Return the configured LLM instance."""
        pass

    @abstractmethod
    def validate_model(self) -> bool:
        """Validate that the model is supported by this client."""
        pass
