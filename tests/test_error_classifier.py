"""Stable error classification for provider / data-source failures."""

from __future__ import annotations

import pytest

from web.error_classifier import classify_provider_exception, worker_error_info
from web.error_codes import (
    PROVIDER_FAILURE_REASONS,
    RETRYABLE_REASONS,
    USER_MESSAGES,
    TerminalReason,
)


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _WrappedError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.response = _Response(status_code)


@pytest.mark.parametrize(
    ("exc", "expected_reason"),
    [
        (TimeoutError("read timed out"), TerminalReason.MODEL_TIMEOUT.value),
        (RuntimeError("Timed out after 30s"), TerminalReason.MODEL_TIMEOUT.value),
        (_StatusError("rate limit hit", 429), TerminalReason.MODEL_RATE_LIMITED.value),
        (_StatusError("unauthorized", 401), TerminalReason.MODEL_AUTH_ERROR.value),
        (_StatusError("forbidden", 403), TerminalReason.MODEL_AUTH_ERROR.value),
        (_StatusError("server error", 500), TerminalReason.MODEL_UNAVAILABLE.value),
        (_StatusError("bad gateway", 502), TerminalReason.MODEL_UNAVAILABLE.value),
        (_StatusError("service unavailable", 503), TerminalReason.MODEL_UNAVAILABLE.value),
        (_StatusError("service unavailable", 503), TerminalReason.MODEL_UNAVAILABLE.value),
        (_WrappedError("upstream busy", 503), TerminalReason.MODEL_UNAVAILABLE.value),
        (RuntimeError("totally opaque"), TerminalReason.WORKER_ERROR.value),
    ],
)
def test_classify_provider_exception_maps_to_stable_reason(exc, expected_reason):
    info = classify_provider_exception(exc)
    assert info.reason == expected_reason
    # Auth errors are never retryable; every other provider failure is.
    if expected_reason == TerminalReason.MODEL_AUTH_ERROR.value:
        assert info.retryable is False
    elif expected_reason in PROVIDER_FAILURE_REASONS:
        assert info.retryable is True
    else:
        assert info.retryable is False


def test_data_source_classification_uses_dedicated_codes():
    info = classify_provider_exception(
        TimeoutError("read timed out"),
        operation="market_data",
    )
    assert info.reason == TerminalReason.DATA_SOURCE_TIMEOUT.value
    assert info.retryable is True


def test_classifier_propagates_provider_metadata():
    info = classify_provider_exception(
        _StatusError("rate limited", 429),
        provider="openai",
        model="gpt-5.5",
        phase="Research Team",
        agent="Bear Researcher",
        attempt=2,
    )
    assert info.provider == "openai"
    assert info.model == "gpt-5.5"
    assert info.phase == "Research Team"
    assert info.agent == "Bear Researcher"
    assert info.attempt == 2


def test_classifier_does_not_leak_original_message_in_user_text():
    secret = "sk-verysecretkey1234567890"
    info = classify_provider_exception(
        RuntimeError(f"call failed api_key={secret}"),
        provider="openai",
        model="gpt-5.5",
    )
    assert secret not in info.user_message
    # User message is taken from the catalog; the classifier only appends
    # a sanitized detail.
    assert USER_MESSAGES[info.reason].split("（")[0] in info.user_message


def test_classifier_safe_detail_truncates_and_strips_newlines():
    info = classify_provider_exception(
        RuntimeError("a\nb\r\nc" * 200),
    )
    assert "\n" not in (info.safe_detail or "")
    assert "\r" not in (info.safe_detail or "")
    assert info.safe_detail is not None
    assert len(info.safe_detail) <= 200


def test_worker_error_info_uses_worker_error_code():
    info = worker_error_info(provider="openai", detail="boom")
    assert info.reason == TerminalReason.WORKER_ERROR.value
    assert info.retryable is False
    assert info.provider == "openai"


def test_retryable_reasons_align_with_provider_failures():
    """Retryable provider failures are a strict subset of provider failures.

    Auth errors are provider failures but are never retryable; worker
    errors are never retryable either.
    """

    assert TerminalReason.MODEL_AUTH_ERROR.value in PROVIDER_FAILURE_REASONS
    assert TerminalReason.MODEL_AUTH_ERROR.value not in RETRYABLE_REASONS
    assert TerminalReason.WORKER_ERROR.value not in RETRYABLE_REASONS
    retryable_provider_failures = RETRYABLE_REASONS & PROVIDER_FAILURE_REASONS
    assert retryable_provider_failures == {
        TerminalReason.MODEL_TIMEOUT.value,
        TerminalReason.MODEL_RATE_LIMITED.value,
        TerminalReason.MODEL_UNAVAILABLE.value,
        TerminalReason.DATA_SOURCE_TIMEOUT.value,
        TerminalReason.DATA_SOURCE_UNAVAILABLE.value,
    }
