"""Roadmap §3.3 R7 — ResolvedRetryPolicy retryableCodes.

dsh spec::  ResolvedRetryPolicy{mode, maxRetries, retryableCodes,
                                initialDelayMs, maxDelayMs, jitterRatio}
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from tradingagents.agent_harness.core.retry import (
    ResolvedRetryPolicy,
    retry_resolved_async,
    retry_resolved_sync,
)
from tradingagents.agent_harness.llm.failure import (
    LlmFailure,
    LlmFailureKind,
    TRANSIENT_KINDS,
)


# --------------------------------------------------------------------------
# ResolvedRetryPolicy dataclass
# --------------------------------------------------------------------------
class TestDefaults:
    def test_default_mode_is_kind(self):
        p = ResolvedRetryPolicy()
        assert p.mode == "kind"

    def test_default_max_retries(self):
        assert ResolvedRetryPolicy().max_retries == 3

    def test_default_retryable_codes_is_TRANSIENT_KINDS(self):
        assert ResolvedRetryPolicy().retryable_codes == frozenset(TRANSIENT_KINDS)

    def test_default_backoff_bounds(self):
        p = ResolvedRetryPolicy()
        assert p.initial_delay_ms == 500
        assert p.max_delay_ms == 30_000
        assert p.jitter_ratio == 0.2

    def test_frozen_immutable(self):
        p = ResolvedRetryPolicy()
        with pytest.raises(Exception):  # FrozenInstanceError
            p.max_retries = 99  # type: ignore[misc]

    def test_empty_retryable_codes_defaults_to_TRANSIENT(self):
        """empty frozenset = treat as 'not specified' → 填 TRANSIENT_KINDS 默认。
        想要白名单零重试 → 显式传 frozenset({LlmFailureKind.RATE_LIMIT}) 等。"""
        p = ResolvedRetryPolicy(retryable_codes=frozenset())
        assert p.retryable_codes == frozenset(TRANSIENT_KINDS)


class TestIsRetryable:
    def test_kind_mode_lets_transient_through(self):
        p = ResolvedRetryPolicy(mode="kind")
        for kind in TRANSIENT_KINDS:
            f = LlmFailure(kind=kind, provider="x", model="m", message="m")
            assert p.is_retryable(f), f"{kind} should be retryable"

    def test_kind_mode_blocks_non_transient(self):
        p = ResolvedRetryPolicy(mode="kind")
        for kind in (
            LlmFailureKind.AUTH, LlmFailureKind.NOT_FOUND,
            LlmFailureKind.CONTEXT_TOO_LONG, LlmFailureKind.INVALID_OUTPUT,
            LlmFailureKind.UNKNOWN,
        ):
            f = LlmFailure(kind=kind, provider="x", model="m", message="m")
            assert not p.is_retryable(f), f"{kind} should NOT be retryable"

    def test_kind_mode_blocks_non_llmfailure(self):
        p = ResolvedRetryPolicy(mode="kind")
        assert not p.is_retryable(ValueError("config bug"))
        assert not p.is_retryable(KeyError("missing field"))

    def test_exception_mode_is_legacy_broad(self):
        p = ResolvedRetryPolicy(mode="exception")
        assert p.is_retryable(ValueError("anything"))
        assert p.is_retryable(ConnectionError("network"))


class TestDelaySeconds:
    def test_exponential_capped_at_max(self):
        """delay_n = min(initial * 2^n, max)"""
        p = ResolvedRetryPolicy(
            initial_delay_ms=500, max_delay_ms=10_000, jitter_ratio=0,
        )
        assert p.delay_seconds(0) == 0.5  # 500
        assert p.delay_seconds(1) == 1.0  # 1000
        assert p.delay_seconds(2) == 2.0  # 2000
        assert p.delay_seconds(3) == 4.0  # 4000
        assert p.delay_seconds(4) == 8.0  # 8000
        assert p.delay_seconds(5) == 10.0  # capped at 10000
        assert p.delay_seconds(10) == 10.0  # still capped

    def test_jitter_within_ratio(self):
        p = ResolvedRetryPolicy(initial_delay_ms=1000, max_delay_ms=60_000, jitter_ratio=0.3)
        for _ in range(50):
            d = p.delay_seconds(2)  # base = 4000ms
            assert 2.8 <= d <= 5.2  # ±30%

    def test_jitter_zero_means_no_jitter(self):
        # initial_delay_ms 默认 500; delay(2) = min(500*4, 30000)/1000 = 2.0
        p = ResolvedRetryPolicy(initial_delay_ms=1000, jitter_ratio=0)
        assert p.delay_seconds(2) == 4.0  # 1000 * 2^2 / 1000 = 4.0


# --------------------------------------------------------------------------
# retry_resolved_sync — sync helper
# --------------------------------------------------------------------------
class TestRetryResolvedSync:
    def test_retries_transient_then_succeeds(self):
        """spec R7:retryableCodes 在 kind 里 → 重试直到 max_retries。"""
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise LlmFailure(
                    kind=LlmFailureKind.RATE_LIMIT,
                    provider="x", model="m",
                    message="429",
                    status_code=429,
                )
            return "ok"

        p = ResolvedRetryPolicy(
            mode="kind", max_retries=3,
            initial_delay_ms=1, max_delay_ms=10, jitter_ratio=0,
        )
        result = retry_resolved_sync(flaky, policy=p)
        assert result == "ok"
        assert len(calls) == 3  # 2 failures + 1 success

    def test_does_not_retry_non_transient(self):
        """spec R7:kind 不在 retryable → 立即 raise,不再重试。"""
        calls = []
        def fail():
            calls.append(1)
            raise LlmFailure(
                kind=LlmFailureKind.AUTH,
                provider="x", model="m", message="401",
                status_code=401,
            )
        p = ResolvedRetryPolicy(mode="kind", max_retries=3)
        with pytest.raises(LlmFailure) as exc:
            retry_resolved_sync(fail, policy=p)
        assert exc.value.kind == LlmFailureKind.AUTH
        assert len(calls) == 1  # 没重试

    def test_max_retries_exhausted_raises_last(self):
        """spec R7:max_retries 用完后,把最后一次失败原样 raise。"""
        calls = []
        def always_fail():
            calls.append(1)
            raise LlmFailure(
                kind=LlmFailureKind.SERVER, provider="x", model="m",
                message="500", status_code=500,
            )
        p = ResolvedRetryPolicy(
            mode="kind", max_retries=2,
            initial_delay_ms=1, max_delay_ms=10, jitter_ratio=0,
        )
        with pytest.raises(LlmFailure) as exc:
            retry_resolved_sync(always_fail, policy=p)
        assert exc.value.kind == LlmFailureKind.SERVER
        assert len(calls) == 3  # initial + 2 retries

    def test_custom_retryable_codes_overrides_default(self):
        """spec R7:retryableCodes 是可配置的 — 让用户能只重试 RATE_LIMIT。"""
        calls = []
        def fail():
            calls.append(1)
            raise LlmFailure(
                kind=LlmFailureKind.SERVER, provider="x", model="m",
                message="500", status_code=500,
            )
        # 只重试 RATE_LIMIT,SERVER 不重试
        p = ResolvedRetryPolicy(
            mode="kind", max_retries=3,
            retryable_codes=frozenset({LlmFailureKind.RATE_LIMIT}),
        )
        with pytest.raises(LlmFailure):
            retry_resolved_sync(fail, policy=p)
        assert len(calls) == 1  # SERVER 不在白名单 → 不重试

    def test_kind_mode_blocks_non_llmfailure(self):
        """spec R7:kind mode 只信任 LlmFailure.kind;普通 Exception 不重试。"""
        calls = []
        def fail():
            calls.append(1)
            raise ValueError("config bug — not retryable")
        p = ResolvedRetryPolicy(mode="kind", max_retries=3)
        with pytest.raises(ValueError):
            retry_resolved_sync(fail, policy=p)
        assert len(calls) == 1


# --------------------------------------------------------------------------
# retry_resolved_async — async helper
# --------------------------------------------------------------------------
class TestRetryResolvedAsync:
    def test_async_retries(self):
        """驱动 retry_resolved_async 直接跑(不依赖 pytest-asyncio)。"""
        import asyncio
        calls = []
        async def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise LlmFailure(
                    kind=LlmFailureKind.NETWORK, provider="x", model="m",
                    message="refused",
                )
            return "ok"
        p = ResolvedRetryPolicy(
            mode="kind", max_retries=2,
            initial_delay_ms=1, max_delay_ms=10, jitter_ratio=0,
        )
        result = asyncio.run(retry_resolved_async(flaky, policy=p))
        assert result == "ok"
        assert len(calls) == 2


# --------------------------------------------------------------------------
# OpenAICompatibleProvider 集成 — verify kind-based retry actually works
# --------------------------------------------------------------------------
class TestProviderRetryIntegration:
    def test_provider_uses_retry_policy(self):
        from tradingagents.agent_harness.llm.openai_provider import OpenAICompatibleProvider
        p = OpenAICompatibleProvider("openai", "gpt-4o-mini", api_key="x")
        # 默认 policy 装好
        assert p._retry_policy is not None
        assert p._retry_policy.mode == "kind"
        assert p._retry_policy.retryable_codes == frozenset(TRANSIENT_KINDS)

    def test_set_retry_policy_replaces(self):
        from tradingagents.agent_harness.llm.openai_provider import OpenAICompatibleProvider
        p = OpenAICompatibleProvider("openai", "gpt-4o-mini", api_key="x")
        custom = ResolvedRetryPolicy(
            mode="kind", max_retries=1,
            retryable_codes=frozenset({LlmFailureKind.RATE_LIMIT}),
        )
        p.set_retry_policy(custom)
        assert p._retry_policy is custom
        assert p._retry_policy.max_retries == 1
