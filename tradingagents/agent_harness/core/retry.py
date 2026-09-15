"""Retry + circuit breaker (v2 spec §7.2 #3/#6).

Three primitives:
- :class:`RetryPolicy` — declarative retry settings
- :func:`retry_async` — exponential backoff helper
- :class:`CircuitBreaker` — open/half-open/closed state machine
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class RetryPolicy:
    max_retries: int = 3
    backoff_seconds: float = 0.5
    exponential: bool = True

    def delay(self, attempt: int) -> float:
        if not self.exponential:
            return self.backoff_seconds
        return self.backoff_seconds * (2 ** attempt)


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    transient_exceptions: tuple[type[BaseException], ...] = (Exception,),
    skip_exceptions: tuple[type[BaseException], ...] = (),
) -> T:
    """Call ``func`` with retries; only re-raises on the last attempt.

    ``skip_exceptions`` lets callers bypass retry for specific exception
    types — used by the orchestrator for ``ProviderError`` because
    network-level failures don't recover from N back-to-back retries
    against the same upstream; the right answer is fallback to another
    provider (handled one layer down in the builtin tools).
    """
    last_err: BaseException | None = None
    for attempt in range(policy.max_retries + 1):
        try:
            return await func()
        except transient_exceptions as e:
            if skip_exceptions and isinstance(e, skip_exceptions):
                # Don't retry, don't double-log — just propagate so the
                # caller (orchestrator) can surface the failure quickly.
                raise
            last_err = e
            if attempt >= policy.max_retries:
                break
            delay = policy.delay(attempt)
            LOGGER.warning("retry attempt=%d delay=%.2fs err=%s", attempt, delay, e)
            await asyncio.sleep(delay)
    assert last_err is not None
    raise last_err


class CircuitState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Open when consecutive failures exceed threshold; probe in half-open."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        return self._state

    def allow(self) -> bool:
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            assert self._opened_at is not None
            if time.time() - self._opened_at >= self.reset_seconds:
                self._state = CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN: allow one probe.
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            LOGGER.warning("circuit breaker OPENED after %d failures", self._failures)



# --------------------------------------------------------------------------
# spec R7 (roadmap §3.3): ResolvedRetryPolicy retryableCodes
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ResolvedRetryPolicy:
    """spec R7 — kind-based retry decision per LLM call.

    dsh design::
        ResolvedRetryPolicy{mode, maxRetries, retryableCodes,
                             initialDelayMs, maxDelayMs, jitterRatio}

    Why this exists
    ---------------
    Old :class:`RetryPolicy` 只有 max_retries + backoff,决策依据是 broad
    ``Exception`` catch-all(对 provider 限流、配额用完、context 过长全一刀切)。
    现在有 :class:`~tradingagents.agent_harness.llm.failure.LlmFailure`
    归一化的 ``kind`` 字段,可以精准决策:

    - RATE_LIMIT    → 重试(429,等下个 quota window)
    - TIMEOUT       → 重试(网络抖动)
    - NETWORK       → 重试(DNS / connection refused)
    - SERVER (5xx)  → 重试(provider 暂时挂了)
    - CONTEXT_TOO_LONG → 不重试(改了 prompt 也放不下)
    - AUTH / NOT_FOUND → 不重试(配置错了,重试也白搭)
    - INVALID_OUTPUT  → 不重试(模型输出坏了,重试可能还是坏)

    Parameters
    ----------
    mode
        ``"kind"`` 用 LlmFailure.kind 决策(spec R7 默认,推荐);
        ``"exception"`` 回退到老 broad-exception 行为(给非 LlmFailure 用)。
    max_retries
        最多重试次数。0 = 不重试,3 = 总共 4 次尝试。
    retryable_codes
        哪些 ``LlmFailureKind`` 值重试。默认 = TRANSIENT_KINDS。
    initial_delay_ms / max_delay_ms
        指数退避边界。delay_n = clamp(initial * 2^n, initial, max)
    jitter_ratio
        在 [-ratio, +ratio] 区间加随机抖动防雷击。0 = 关闭抖动。
    """
    mode: str = "kind"
    max_retries: int = 3
    retryable_codes: "frozenset" = frozenset()  # filled in __post_init__ below
    initial_delay_ms: int = 500
    max_delay_ms: int = 30_000
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        # dataclass(frozen=True) 不允许直接赋值,所以用 object.__setattr__
        if not self.retryable_codes:
            try:
                from tradingagents.agent_harness.llm.failure import TRANSIENT_KINDS
            except ImportError:
                TRANSIENT_KINDS = frozenset()
            object.__setattr__(self, "retryable_codes", TRANSIENT_KINDS)

    def is_retryable(self, exc: BaseException) -> bool:
        """Decide whether ``exc`` should trigger another attempt.

        ``"kind"`` mode: only :class:`LlmFailure` whose ``kind`` is in
        ``retryable_codes``. Non-LlmFailure 一律不重试(避免老的 broad
        Exception catch 把 CONFIG 错误也重试)。
        ``"exception"`` mode: any ``Exception``(legacy 行为)。
        """
        if self.mode == "exception":
            return isinstance(exc, Exception)
        # kind mode
        if not hasattr(exc, "kind"):
            return False
        kind = getattr(exc, "kind", None)
        return kind in self.retryable_codes

    def delay_seconds(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped at ``max_delay_ms``.

        ``attempt`` 是 0-based(第 1 次重试用 attempt=0)。
        """
        import random
        base = min(
            self.initial_delay_ms * (2 ** attempt),
            self.max_delay_ms,
        )
        if self.jitter_ratio > 0:
            jitter = base * self.jitter_ratio * random.uniform(-1.0, 1.0)
            base = max(0.0, base + jitter)
        return base / 1000.0


async def retry_resolved_async(
    func,
    *,
    policy: ResolvedRetryPolicy,
):
    """Retry helper driven by :class:`ResolvedRetryPolicy`.

    与 :func:`retry_async`(老 RetryPolicy 走 exception 类元组)并存,工具
    层不动;LLM 层(``OpenAICompatibleProvider.complete``)用这个。
    """
    import asyncio
    last_err: BaseException | None = None
    for attempt in range(policy.max_retries + 1):
        try:
            return await func()
        except BaseException as e:
            if not policy.is_retryable(e):
                raise
            last_err = e
            if attempt >= policy.max_retries:
                break
            delay = policy.delay_seconds(attempt)
            LOGGER.warning(
                "retry_resolved attempt=%d delay=%.3fs kind=%s err=%s",
                attempt, delay, getattr(e, "kind", "?"), e,
            )
            await asyncio.sleep(delay)
    assert last_err is not None
    raise last_err


def retry_resolved_sync(
    func,
    *,
    policy: ResolvedRetryPolicy,
):
    """Sync counterpart of :func:`retry_resolved_async`.

    Used by sync LLM provider entry points (``OpenAICompatibleProvider.complete``)
    which are called from inside async agent methods but themselves are sync.
    """
    import time
    last_err: BaseException | None = None
    for attempt in range(policy.max_retries + 1):
        try:
            return func()
        except BaseException as e:
            if not policy.is_retryable(e):
                raise
            last_err = e
            if attempt >= policy.max_retries:
                break
            delay = policy.delay_seconds(attempt)
            LOGGER.warning(
                "retry_resolved_sync attempt=%d delay=%.3fs kind=%s err=%s",
                attempt, delay, getattr(e, "kind", "?"), e,
            )
            time.sleep(delay)
    assert last_err is not None
    raise last_err
