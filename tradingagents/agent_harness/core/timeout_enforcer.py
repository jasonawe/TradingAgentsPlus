"""Timeout enforcement for tool / LLM / request calls (v3 spec §7.2 #4).

Three guarantees:

- 每个 tool 调用的 wall-clock 上限由 ``ToolSchema.timeout_seconds`` 控制
- 每个 LLM 调用的 wall-clock 上限由 ``HarnessConfig.llm_timeout_seconds`` 控制
- 每个 HTTP request 上限由 provider 自己配置

``TimeoutEnforcer.enforce(coro, timeout_seconds)`` wraps an awaitable
in :func:`asyncio.wait_for` and raises :class:`CallTimeoutError` on
expiry.  Sync functions go through ``asyncio.to_thread`` so the same
API serves both.

Errors:
- :class:`CallTimeoutError` (subclass of asyncio.TimeoutError) — caller
  sees a uniform error type regardless of sync / async source.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)


class CallTimeoutError(asyncio.TimeoutError):
    """Raised when an enforced call exceeds its budget.

    Inherits from :class:`asyncio.TimeoutError` so existing ``except
    TimeoutError`` blocks still match.  ``elapsed`` / `` ``budget`` /
    ``op`` attributes make it possible to log / surface meaningful
    context without losing the underlying behaviour.
    """

    def __init__(self, op: str, budget: float, elapsed: float) -> None:
        super().__init__(f"{op} timed out after {elapsed:.3f}s (budget={budget:.3f}s)")
        self.op = op
        self.budget = budget
        self.elapsed = elapsed


@dataclass
class TimeoutStats:
    """Aggregate stats for an enforcer — useful for observability."""
    invoked: int = 0
    succeeded: int = 0
    timed_out: int = 0
    failed_other: int = 0
    total_elapsed: float = 0.0

    def as_dict(self) -> dict:
        return {
            "invoked": self.invoked,
            "succeeded": self.succeeded,
            "timed_out": self.timed_out,
            "failed_other": self.failed_other,
            "total_elapsed": round(self.total_elapsed, 3),
        }


class TimeoutEnforcer:
    """Wrap an awaitable / callable with a wall-clock budget.

    Parameters
    ----------
    op
        Operation label used in error messages (``"tool.get_quote"`` etc).
    default_timeout_seconds
        Default per-call budget.  ``0`` disables enforcement — the
        wrapped call runs without a cap.
    """

    def __init__(self, *, op: str, default_timeout_seconds: float = 30.0) -> None:
        if default_timeout_seconds < 0:
            raise ValueError("default_timeout_seconds must be >= 0")
        self.op = op
        self.default_timeout_seconds = float(default_timeout_seconds)
        self.stats = TimeoutStats()

    # ------------------------------------------------------------------
    # Async — single coroutine
    # ------------------------------------------------------------------
    async def enforce(
        self, awaitable, *, timeout_seconds: float | None = None,
    ) -> object:
        """Run ``awaitable`` with a wall-clock cap.

        ``timeout_seconds=None`` falls back to ``self.default_timeout_seconds``.
        ``0`` disables enforcement.
        """
        budget = self.default_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        self.stats.invoked += 1
        t0 = time.monotonic()
        try:
            if budget <= 0:
                result = await awaitable
            else:
                result = await asyncio.wait_for(awaitable, timeout=budget)
        except asyncio.TimeoutError as e:
            elapsed = time.monotonic() - t0
            self.stats.timed_out += 1
            self.stats.total_elapsed += elapsed
            LOGGER.warning("%s timed out after %.3fs (budget=%.3fs)", self.op, elapsed, budget)
            raise CallTimeoutError(self.op, budget, elapsed) from e
        except BaseException:
            self.stats.failed_other += 1
            self.stats.total_elapsed += time.monotonic() - t0
            raise
        self.stats.succeeded += 1
        self.stats.total_elapsed += time.monotonic() - t0
        return result

    # ------------------------------------------------------------------
    # Sync — function call in a worker thread
    # ------------------------------------------------------------------
    async def enforce_sync(
        self, func, args: tuple = (), *, kwargs: dict | None = None,
        timeout_seconds: float | None = None,
    ) -> object:
        """Run ``func(*args, **kwargs)`` in a worker thread with a budget."""
        kwargs = kwargs or {}
        budget = self.default_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        self.stats.invoked += 1
        t0 = time.monotonic()
        try:
            if budget <= 0:
                result = await asyncio.to_thread(func, *args, **kwargs)
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(func, *args, **kwargs),
                    timeout=budget,
                )
        except asyncio.TimeoutError as e:
            elapsed = time.monotonic() - t0
            self.stats.timed_out += 1
            self.stats.total_elapsed += elapsed
            LOGGER.warning("%s (sync) timed out after %.3fs (budget=%.3fs)", self.op, elapsed, budget)
            raise CallTimeoutError(self.op, budget, elapsed) from e
        except BaseException:
            self.stats.failed_other += 1
            self.stats.total_elapsed += time.monotonic() - t0
            raise
        self.stats.succeeded += 1
        self.stats.total_elapsed += time.monotonic() - t0
        return result

    def reset_stats(self) -> None:
        self.stats = TimeoutStats()


# ---------------------------------------------------------------------------
# Helpers — module-level convenience
# ---------------------------------------------------------------------------
async def with_timeout(
    awaitable, *, op: str, timeout_seconds: float = 30.0,
) -> object:
    """One-shot helper: enforce timeout on a single awaitable."""
    enforcer = TimeoutEnforcer(op=op, default_timeout_seconds=timeout_seconds)
    return await enforcer.enforce(awaitable)


def is_coro_or_callable(obj) -> bool:
    """True when ``obj`` is awaitable / callable (anything ``enforce`` accepts)."""
    return inspect.iscoroutine(obj) or inspect.iscoroutinefunction(obj) or callable(obj)
