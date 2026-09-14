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
) -> T:
    """Call ``func`` with retries; only re-raises on the last attempt."""
    last_err: BaseException | None = None
    for attempt in range(policy.max_retries + 1):
        try:
            return await func()
        except transient_exceptions as e:
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
