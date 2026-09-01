"""Translate provider/data exceptions into stable error structures.

The classifier never includes the original exception text, headers, or
any payload that might contain user data. Returned ``ProviderErrorInfo``
instances drive the manager's transition to a failed terminal state and
populate ``web_runs.error_code``, ``error_message``, and the
``failed_*`` diagnostic columns.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from .error_codes import USER_MESSAGES, TerminalReason

logger = logging.getLogger(__name__)


_SAFE_CODE = re.compile(r"[^a-zA-Z0-9_.-]")


@dataclass(frozen=True)
class ProviderErrorInfo:
    """Structured representation of one classified failure."""

    reason: str
    user_message: str
    provider: str | None = None
    model: str | None = None
    phase: str | None = None
    agent: str | None = None
    attempt: int | None = None
    safe_detail: str | None = None
    retryable: bool = False

    def to_failure_kwargs(self) -> dict[str, Any]:
        """Mapping consumed by ``RunManager.fail_run`` and the artifact writer."""

        return {
            "error_code": self.reason,
            "error_message": self.user_message,
            "failed_phase": self.phase,
            "failed_agent": self.agent,
            "failed_provider": self.provider,
            "failed_model": self.model,
            "active_attempt": self.attempt,
            "retryable": self.retryable,
        }


def _sanitize_detail(value: Any, limit: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = _SAFE_CODE.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text[:limit]


def _looks_like_timeout(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    module = type(exc).__module__.lower()
    full = f"{module}.{name}"
    if "timeout" in name or "timeout" in full:
        return True
    message = str(exc).lower()
    return bool("timed out" in message or "timeout" in message)


def _looks_like_rate_limit(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "ratelimit" in name or "rate_limit" in name:
        return True
    if "rate limit" in message or "too many requests" in message:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 429:
        return True
    response = getattr(exc, "response", None)
    return bool(response is not None and getattr(response, "status_code", None) == 429)


def _looks_like_auth(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "auth" in name and ("error" in name or "fail" in name):
        return True
    if "authentication" in message or "unauthorized" in message:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in (401, 403):
        return True
    response = getattr(exc, "response", None)
    return bool(response is not None and getattr(response, "status_code", None) in (401, 403))


def _looks_like_unavailable(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if any(token in name for token in ("serviceunavailable", "apierror", "internalserver")):
        return True
    if any(token in message for token in ("service unavailable", "internal server error", "bad gateway", "gateway timeout", "overloaded")):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in (500, 502, 503, 504):
        return True
    response = getattr(exc, "response", None)
    return bool(response is not None and getattr(response, "status_code", None) in (500, 502, 503, 504))


def _extract_status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def classify_provider_exception(
    exc: BaseException,
    *,
    provider: str | None = None,
    model: str | None = None,
    phase: str | None = None,
    agent: str | None = None,
    attempt: int | None = None,
    operation: str | None = None,
) -> ProviderErrorInfo:
    """Map one provider exception to a stable, user-safe error."""

    safe_detail = _sanitize_detail(getattr(exc, "message", None) or str(exc) or type(exc).__name__)
    status_code = _extract_status_code(exc)
    detail_with_status = (
        f"http={status_code} {safe_detail}" if status_code is not None else safe_detail
    )

    op = (operation or "").lower()
    is_data_source = op in {"market_data", "data_source"}

    if _looks_like_timeout(exc):
        reason = TerminalReason.DATA_SOURCE_TIMEOUT.value if is_data_source else TerminalReason.MODEL_TIMEOUT.value
        retryable = True
    elif _looks_like_rate_limit(exc):
        reason = TerminalReason.MODEL_RATE_LIMITED.value
        retryable = True
    elif _looks_like_auth(exc):
        reason = TerminalReason.MODEL_AUTH_ERROR.value
        retryable = False
    elif _looks_like_unavailable(exc):
        reason = TerminalReason.DATA_SOURCE_UNAVAILABLE.value if is_data_source else TerminalReason.MODEL_UNAVAILABLE.value
        retryable = True
    elif is_data_source and _looks_like_timeout(exc) is False:
        reason = TerminalReason.DATA_SOURCE_UNAVAILABLE.value
        retryable = True
    else:
        reason = TerminalReason.WORKER_ERROR.value
        retryable = False

    user_message = USER_MESSAGES.get(reason, USER_MESSAGES[TerminalReason.WORKER_ERROR.value])
    if safe_detail and reason in {
        TerminalReason.MODEL_TIMEOUT.value,
        TerminalReason.MODEL_UNAVAILABLE.value,
        TerminalReason.DATA_SOURCE_TIMEOUT.value,
        TerminalReason.DATA_SOURCE_UNAVAILABLE.value,
    }:
        # Append the safe detail so users can see WHY (e.g. http=504) without
        # leaking the original prompt or response.
        user_message = f"{user_message}（{detail_with_status}）"[:500]

    info = ProviderErrorInfo(
        reason=reason,
        user_message=user_message,
        provider=provider,
        model=model,
        phase=phase,
        agent=agent,
        attempt=attempt,
        safe_detail=safe_detail,
        retryable=retryable,
    )
    logger.warning(
        "provider_failure reason=%s provider=%s model=%s phase=%s agent=%s attempt=%s status=%s",
        reason,
        provider,
        model,
        phase,
        agent,
        attempt,
        status_code,
    )
    return info


def worker_error_info(
    *,
    provider: str | None = None,
    model: str | None = None,
    phase: str | None = None,
    agent: str | None = None,
    detail: Any = None,
) -> ProviderErrorInfo:
    """Wrap an unclassified worker error into the same shape as provider failures."""

    return ProviderErrorInfo(
        reason=TerminalReason.WORKER_ERROR.value,
        user_message=USER_MESSAGES[TerminalReason.WORKER_ERROR.value],
        provider=provider,
        model=model,
        phase=phase,
        agent=agent,
        attempt=None,
        safe_detail=_sanitize_detail(detail),
        retryable=False,
    )


__all__ = ["ProviderErrorInfo", "classify_provider_exception", "worker_error_info"]
