"""LlmFailure normalized exception — classification + dict shape.

P1 priority item: every LLM call must surface a structured failure so
the orchestrator / frontend can distinguish rate-limit / context-too-long
/ auth / network errors without parsing strings.
"""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.llm.failure import (
    LlmFailure,
    LlmFailureKind,
    TRANSIENT_KINDS,
    classify_llm_error,
)


# --------------------------------------------------------------------------
# LlmFailure basics
# --------------------------------------------------------------------------
class TestLlmFailure:
    def test_basic_fields_round_trip(self):
        f = LlmFailure(
            kind=LlmFailureKind.RATE_LIMIT,
            provider="openai",
            model="gpt-4o",
            message="429 too many requests",
            retry_after_s=12.5,
            status_code=429,
        )
        assert f.kind is LlmFailureKind.RATE_LIMIT
        assert f.provider == "openai"
        assert f.model == "gpt-4o"
        assert f.retry_after_s == 12.5
        assert f.status_code == 429
        assert f.is_transient is True

    def test_str_includes_kind_and_provider(self):
        f = LlmFailure(
            kind=LlmFailureKind.AUTH, provider="anthropic", model="claude-3",
            message="invalid api key",
        )
        s = str(f)
        assert "[auth]" in s
        assert "anthropic" in s
        assert "claude-3" in s
        assert "invalid api key" in s

    def test_str_includes_optional_status_and_retry(self):
        f = LlmFailure(
            kind=LlmFailureKind.RATE_LIMIT, provider="x", model="m",
            message="m", status_code=429, retry_after_s=30.0,
        )
        s = str(f)
        assert "status=429" in s
        assert "retry_after=30.0s" in s

    def test_is_exception(self):
        f = LlmFailure(
            kind=LlmFailureKind.NETWORK, provider="x", model="m", message="m"
        )
        assert isinstance(f, Exception)
        try:
            raise f
        except LlmFailure as caught:
            assert caught is f

    def test_to_dict_shape_is_stable(self):
        f = LlmFailure(
            kind=LlmFailureKind.CONTEXT_TOO_LONG, provider="openai", model="gpt-4o",
            message="prompt too long", retry_after_s=None, raw_error="raw",
            status_code=413,
        )
        d = f.to_dict()
        assert d["kind"] == "context_too_long"
        assert d["provider"] == "openai"
        assert d["model"] == "gpt-4o"
        assert d["retry_after_s"] is None
        assert d["status_code"] == 413
        assert d["transient"] is False
        assert d["raw_error"] == "raw"

    def test_non_transient_kinds(self):
        for kind in (
            LlmFailureKind.AUTH, LlmFailureKind.NOT_FOUND,
            LlmFailureKind.CONTEXT_TOO_LONG, LlmFailureKind.INVALID_OUTPUT,
            LlmFailureKind.UNKNOWN,
        ):
            f = LlmFailure(kind=kind, provider="x", model="m", message="m")
            assert f.is_transient is False, f"{kind} should not be transient"

    def test_transient_kinds_constant(self):
        # Documented: which kinds the harness will retry.
        assert LlmFailureKind.RATE_LIMIT in TRANSIENT_KINDS
        assert LlmFailureKind.TIMEOUT in TRANSIENT_KINDS
        assert LlmFailureKind.NETWORK in TRANSIENT_KINDS
        assert LlmFailureKind.SERVER in TRANSIENT_KINDS


# --------------------------------------------------------------------------
# Classifier — exception-name → kind
# --------------------------------------------------------------------------
class TestClassifyByExceptionName:
    @pytest.mark.parametrize(
        "exc_name,expected_kind",
        [
            ("RateLimitError", LlmFailureKind.RATE_LIMIT),
            ("AuthenticationError", LlmFailureKind.AUTH),
            ("PermissionDeniedError", LlmFailureKind.AUTH),
            ("NotFoundError", LlmFailureKind.NOT_FOUND),
            ("TimeoutError", LlmFailureKind.TIMEOUT),
            ("APITimeoutError", LlmFailureKind.TIMEOUT),
            ("APIConnectionError", LlmFailureKind.NETWORK),
            ("NetworkError", LlmFailureKind.NETWORK),
            ("ContextWindowExceededError", LlmFailureKind.CONTEXT_TOO_LONG),
            ("InvalidRequestError", LlmFailureKind.INVALID_OUTPUT),
        ],
    )
    def test_known_exception_names(self, exc_name, expected_kind):
        # Build a fresh Exception subclass whose __name__ matches the SDK class
        # we want to simulate (e.g. "RateLimitError", "TimeoutError").
        cls = type(exc_name, (Exception,), {})
        real_exc = cls("msg")
        f = classify_llm_error(real_exc, provider="p", model="m")
        assert f.kind is expected_kind, f"{exc_name} -> {f.kind.value}"
        assert f.provider == "p"
        assert f.model == "m"

    def test_status_code_overrides_unknown_name(self):
        exc = RuntimeError("something weird")
        f = classify_llm_error(exc, provider="p", model="m", status_code=429)
        assert f.kind is LlmFailureKind.RATE_LIMIT

    def test_unknown_falls_back_to_unknown(self):
        exc = RuntimeError("completely opaque")
        f = classify_llm_error(exc, provider="p", model="m")
        assert f.kind is LlmFailureKind.UNKNOWN


# --------------------------------------------------------------------------
# Classifier — string-sniffing fallback
# --------------------------------------------------------------------------
class TestClassifyByMessageString:
    @pytest.mark.parametrize(
        "msg,expected_kind",
        [
            ("Rate limit reached", LlmFailureKind.RATE_LIMIT),
            ("429: rate_limit exceeded", LlmFailureKind.RATE_LIMIT),
            ("too many requests, slow down", LlmFailureKind.RATE_LIMIT),
            ("This model's maximum context length is 8192 tokens",
             LlmFailureKind.CONTEXT_TOO_LONG),
            ("context_length_exceeded", LlmFailureKind.CONTEXT_TOO_LONG),
            ("Request timed out", LlmFailureKind.TIMEOUT),
            ("Connection refused", LlmFailureKind.NETWORK),
            ("DNS resolution failed", LlmFailureKind.NETWORK),
            ("Invalid API key provided", LlmFailureKind.AUTH),
            ("Unauthorized: token expired", LlmFailureKind.AUTH),
            ("Internal server error 502", LlmFailureKind.SERVER),
        ],
    )
    def test_string_sniff(self, msg, expected_kind):
        exc = RuntimeError(msg)
        f = classify_llm_error(exc, provider="p", model="m")
        assert f.kind is expected_kind, f"{msg!r} -> {f.kind.value}"


# --------------------------------------------------------------------------
# Classifier — message truncation + retry_after passthrough
# --------------------------------------------------------------------------
class TestClassifierDetails:
    def test_message_truncated_to_500(self):
        exc = RuntimeError("X" * 5000)
        f = classify_llm_error(exc, provider="p", model="m")
        assert len(f.message) == 500
        assert len(f.raw_error) == 2000  # raw_error has its own cap

    def test_retry_after_passthrough(self):
        exc = RuntimeError("rate limit")
        f = classify_llm_error(exc, provider="p", model="m", retry_after_s=42.5)
        assert f.retry_after_s == 42.5
        assert f.is_transient is True

    def test_status_code_passthrough(self):
        exc = RuntimeError("oops")
        f = classify_llm_error(exc, provider="p", model="m", status_code=503)
        assert f.status_code == 503
        assert f.kind is LlmFailureKind.SERVER  # 503 in status-code map
