"""Normalized LLM failure model — uniform error shape for all providers.

Why this exists
---------------
Today, LLM exceptions are raw ``Exception`` instances bubbling up from
provider SDKs. Callers (orchestrator + 6 sub-agents + L3 judge + frontend)
see opaque strings like "RateLimitError: 429" or "context_length_exceeded"
and have no reliable way to:

- distinguish transient (retryable) vs permanent failures
- surface ``retry_after`` hints
- track per-kind metrics
- tell the frontend "context too long" vs "rate limit" vs "auth error"

The fix is a single ``LlmFailure`` exception with a structured
``LlmFailureKind`` enum + provider/model/raw-error context, raised from
every ``LLMProvider.complete()`` call.  Callers ``except LlmFailure``
and inspect ``kind`` for routing decisions.

Wire-in points
--------------
- ``OpenAICompatibleProvider.complete`` wraps the underlying client call
  and re-raises as ``LlmFailure`` when the SDK raises.
- ``Orchestrator._llm_plan`` / ``_llm_synthesize`` log the kind and let
  the heuristic fallback kick in (existing behaviour, just with better logs).
- New: ``LlmFailure.kind`` is emitted in the SSE ``error`` event payload
  so the frontend can render specific messages / cooldown timers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

LOGGER = logging.getLogger(__name__)


class LlmFailureKind(str, Enum):
    """Classified LLM failure categories.

    Used both for retry policy (transient vs permanent) and for the
    frontend to render specific messages ("rate limit, retry in 30s").
    """

    RATE_LIMIT = "rate_limit"          # 429 from provider — transient
    CONTEXT_TOO_LONG = "context_too_long"  # prompt + output > model limit
    AUTH = "auth"                      # 401/403 — config issue, not transient
    NOT_FOUND = "not_found"            # 404 — model name typo, etc.
    TIMEOUT = "timeout"                # network/SDK timeout
    NETWORK = "network"                # connection refused, DNS, etc.
    INVALID_OUTPUT = "invalid_output"  # LLM returned non-parseable output
    SERVER = "server"                  # 5xx from provider
    UNKNOWN = "unknown"                # catch-all


# Transient kinds that are worth retrying with backoff.
TRANSIENT_KINDS = frozenset({
    LlmFailureKind.RATE_LIMIT,
    LlmFailureKind.TIMEOUT,
    LlmFailureKind.NETWORK,
    LlmFailureKind.SERVER,
})


@dataclass
class LlmFailure(Exception):
    """Normalized LLM failure exception.

    Carries classification + provider/model + raw error so callers can
    route, retry, or render without parsing strings.
    """

    kind: LlmFailureKind
    provider: str
    model: str
    message: str
    retry_after_s: float | None = None
    raw_error: str | None = None
    status_code: int | None = None

    def __post_init__(self) -> None:
        # Initialize Exception with a human-readable string.
        # ``str(self)`` still works naturally.
        super().__init__(self._build_str())

    def _build_str(self) -> str:
        bits = [f"[{self.kind.value}] {self.provider}/{self.model}: {self.message}"]
        if self.status_code is not None:
            bits.append(f"(status={self.status_code})")
        if self.retry_after_s is not None:
            bits.append(f"(retry_after={self.retry_after_s:.1f}s)")
        return " ".join(bits)

    @property
    def is_transient(self) -> bool:
        """True if a retry with backoff has a chance of succeeding."""
        return self.kind in TRANSIENT_KINDS

    def to_dict(self) -> dict[str, Any]:
        """Stable dict shape for SSE / logs / metrics."""
        return {
            "kind": self.kind.value,
            "provider": self.provider,
            "model": self.model,
            "message": self.message,
            "retry_after_s": self.retry_after_s,
            "status_code": self.status_code,
            "transient": self.is_transient,
            "raw_error": self.raw_error,
        }


# --------------------------------------------------------------------------
# Classification — best-effort mapping from arbitrary SDK exceptions to
# ``LlmFailureKind``.  We deliberately do NOT import each provider's SDK
# (langchain_anthropic, openai, etc.) so this module stays light.  Instead
# we inspect ``type(e).__name__`` and ``str(e)`` for known patterns.
# --------------------------------------------------------------------------

_KIND_BY_EXCEPTION_NAME = {
    "RateLimitError": LlmFailureKind.RATE_LIMIT,
    "AuthenticationError": LlmFailureKind.AUTH,
    "PermissionDeniedError": LlmFailureKind.AUTH,
    "NotFoundError": LlmFailureKind.NOT_FOUND,
    "TimeoutError": LlmFailureKind.TIMEOUT,
    "APITimeoutError": LlmFailureKind.TIMEOUT,
    "APIConnectionError": LlmFailureKind.NETWORK,
    "NetworkError": LlmFailureKind.NETWORK,
    "ContextWindowExceededError": LlmFailureKind.CONTEXT_TOO_LONG,
    "InvalidRequestError": LlmFailureKind.INVALID_OUTPUT,
}

_KIND_BY_STATUS_CODE = {
    401: LlmFailureKind.AUTH,
    403: LlmFailureKind.AUTH,
    404: LlmFailureKind.NOT_FOUND,
    408: LlmFailureKind.TIMEOUT,
    413: LlmFailureKind.CONTEXT_TOO_LONG,
    429: LlmFailureKind.RATE_LIMIT,
    500: LlmFailureKind.SERVER,
    502: LlmFailureKind.SERVER,
    503: LlmFailureKind.SERVER,
    504: LlmFailureKind.SERVER,
}


def classify_llm_error(
    exc: BaseException,
    *,
    provider: str,
    model: str,
    status_code: int | None = None,
    retry_after_s: float | None = None,
) -> LlmFailure:
    """Convert any exception raised by an LLM SDK call to ``LlmFailure``.

    The original ``exc`` is *not* chained (``raise ... from exc`` would
    surface the SDK traceback, which is useful for debugging but noisy
    for end users).  The original message is preserved in ``raw_error``.
    """
    name = type(exc).__name__
    msg = str(exc) or name
    kind = _KIND_BY_EXCEPTION_NAME.get(name)
    if kind is None:
        kind = _KIND_BY_STATUS_CODE.get(status_code or 0, LlmFailureKind.UNKNOWN)
    # Some SDKs raise generic Exception with a string that hints at the kind.
    if kind == LlmFailureKind.UNKNOWN:
        low = msg.lower()
        if "rate limit" in low or "rate_limit" in low or "too many requests" in low:
            kind = LlmFailureKind.RATE_LIMIT
        elif "context length" in low or "context_length" in low or "maximum context" in low:
            kind = LlmFailureKind.CONTEXT_TOO_LONG
        elif "timeout" in low or "timed out" in low:
            kind = LlmFailureKind.TIMEOUT
        elif "auth" in low or "api key" in low or "unauthorized" in low:
            kind = LlmFailureKind.AUTH
        elif "connect" in low or "network" in low or "dns" in low:
            kind = LlmFailureKind.NETWORK
        elif "internal server" in low or " 5xx" in low:
            kind = LlmFailureKind.SERVER
    LOGGER.debug("classify_llm_error: %s.%s -> %s", provider, name, kind.value)
    return LlmFailure(
        kind=kind,
        provider=provider,
        model=model,
        message=msg[:500],
        retry_after_s=retry_after_s,
        raw_error=msg[:2000],
        status_code=status_code,
    )
