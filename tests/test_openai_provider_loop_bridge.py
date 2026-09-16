"""Regression: §7.2 #4 timeout enforcer inside FastAPI running loop.

``LLMProvider.complete`` is a sync ABC method, but internally it needs to
drive the async :class:`TimeoutEnforcer`.  When the call originates from
inside a running event loop (FastAPI handler / asyncio.Task), the naive
``asyncio.run(coro)`` raises ``RuntimeError: cannot be called from a
running event loop`` — which silently breaks ``stream_chat`` plan /
synthesize stages.

``_run_coro_sync`` falls back to running the coroutine on a fresh worker
thread with its own loop.  This module verifies both paths.
"""
from __future__ import annotations

import asyncio

import pytest

from tradingagents.agent_harness.llm.openai_provider import _run_coro_sync


def _echo(value):
    async def _coro():
        await asyncio.sleep(0)
        return value

    return _coro()


def test_no_running_loop_uses_asyncio_run():
    """When called outside any loop, behaves like ``asyncio.run``."""
    result = _run_coro_sync(_echo(42))
    assert result == 42


@pytest.mark.asyncio
async def test_running_loop_uses_worker_thread():
    """When called from inside a running loop, still returns the value."""
    result = _run_coro_sync(_echo("ok"))
    assert result == "ok"


@pytest.mark.asyncio
async def test_running_loop_propagates_exception():
    """Exceptions raised in the coroutine still bubble up."""

    async def _bad():
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _run_coro_sync(_bad())


@pytest.mark.asyncio
async def test_nested_concurrent_calls_inside_running_loop():
    """Multiple concurrent calls from inside a loop all succeed."""
    results = await asyncio.gather(
        asyncio.to_thread(_run_coro_sync, _echo(1)),
        asyncio.to_thread(_run_coro_sync, _echo(2)),
        asyncio.to_thread(_run_coro_sync, _echo(3)),
    )
    assert results == [1, 2, 3]
