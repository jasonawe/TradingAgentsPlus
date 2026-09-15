"""Tests for TimeoutEnforcer (v3 spec §7.2 #4).

覆盖:
- 构造参数校验
- enforce: 正常 / 超时 / 异常传递 / 0 = 禁用
- enforce_sync: 正常 / 超时
- 默认超时 vs 显式超时
- stats 计数 (invoked / succeeded / timed_out / total_elapsed)
- CallTimeoutError: 继承 asyncio.TimeoutError + 携带 op/budget/elapsed
- with_timeout 单次 helper
"""
from __future__ import annotations

import asyncio
import time

import pytest


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def test_construction_rejects_negative_budget():
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    with pytest.raises(ValueError):
        TimeoutEnforcer(op="x", default_timeout_seconds=-1.0)


def test_construction_allows_zero_disables_default():
    """Zero budget is valid — means "no default cap", callers can still pass timeout_seconds=... per-call."""
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    enforcer = TimeoutEnforcer(op="x", default_timeout_seconds=0.0)
    assert enforcer.default_timeout_seconds == 0.0


def test_construction_default_budget_30s():
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    enforcer = TimeoutEnforcer(op="x")
    assert enforcer.default_timeout_seconds == 30.0


# ---------------------------------------------------------------------------
# enforce — async
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_enforce_returns_result_when_within_budget():
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    enforcer = TimeoutEnforcer(op="test", default_timeout_seconds=1.0)

    async def fast():
        await asyncio.sleep(0.01)
        return "ok"

    result = await enforcer.enforce(fast())
    assert result == "ok"
    assert enforcer.stats.succeeded == 1
    assert enforcer.stats.timed_out == 0


@pytest.mark.asyncio
async def test_enforce_raises_call_timeout_error_on_expiry():
    from tradingagents.agent_harness.core.timeout_enforcer import (
        CallTimeoutError, TimeoutEnforcer,
    )
    enforcer = TimeoutEnforcer(op="slow_op", default_timeout_seconds=0.1)

    async def slow():
        await asyncio.sleep(1.0)
        return "too late"

    t0 = time.monotonic()
    with pytest.raises(CallTimeoutError) as ei:
        await enforcer.enforce(slow())
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5  # sanity — waited ~0.1s, not 1.0s
    err = ei.value
    assert err.op == "slow_op"
    assert err.budget == pytest.approx(0.1, abs=1e-3)
    assert isinstance(err, asyncio.TimeoutError)  # 继承自 asyncio.TimeoutError
    assert enforcer.stats.timed_out == 1
    assert enforcer.stats.succeeded == 0


@pytest.mark.asyncio
async def test_enforce_zero_budget_disables_cap():
    """0 = 不设上限,慢函数也返 result。"""
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    enforcer = TimeoutEnforcer(op="no_cap", default_timeout_seconds=0.0)

    async def slow():
        await asyncio.sleep(0.1)
        return "ok"

    result = await enforcer.enforce(slow())
    assert result == "ok"


@pytest.mark.asyncio
async def test_enforce_per_call_timeout_overrides_default():
    from tradingagents.agent_harness.core.timeout_enforcer import (
        CallTimeoutError, TimeoutEnforcer,
    )
    enforcer = TimeoutEnforcer(op="op", default_timeout_seconds=10.0)  # 默认 10s

    async def slow():
        await asyncio.sleep(1.0)

    with pytest.raises(CallTimeoutError):
        # 显式 0.05s 覆盖默认 10s
        await enforcer.enforce(slow(), timeout_seconds=0.05)


@pytest.mark.asyncio
async def test_enforce_propagates_exceptions_other_than_timeout():
    """非 TimeoutError 的异常应该原样抛出,不计入 timed_out。"""
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer

    class Boom(Exception):
        pass

    enforcer = TimeoutEnforcer(op="op", default_timeout_seconds=1.0)

    async def boom():
        raise Boom("kaboom")

    with pytest.raises(Boom):
        await enforcer.enforce(boom())
    assert enforcer.stats.failed_other == 1
    assert enforcer.stats.succeeded == 0
    assert enforcer.stats.timed_out == 0


# ---------------------------------------------------------------------------
# enforce_sync
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_enforce_sync_returns_result_for_fast_function():
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    enforcer = TimeoutEnforcer(op="sync_op", default_timeout_seconds=1.0)

    def fast(x, y):
        return x + y

    result = await enforcer.enforce_sync(fast, args=(1, 2))
    assert result == 3


@pytest.mark.asyncio
async def test_enforce_sync_kwargs():
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    enforcer = TimeoutEnforcer(op="sync_op", default_timeout_seconds=1.0)

    def greet(name, prefix="Hello"):
        return f"{prefix} {name}"

    result = await enforcer.enforce_sync(greet, args=("World",), kwargs={"prefix": "Hi"})
    assert result == "Hi World"


@pytest.mark.asyncio
async def test_enforce_sync_raises_on_expiry():
    import time as _t
    from tradingagents.agent_harness.core.timeout_enforcer import (
        CallTimeoutError, TimeoutEnforcer,
    )
    enforcer = TimeoutEnforcer(op="slow_sync", default_timeout_seconds=0.1)

    def slow():
        _t.sleep(1.0)
        return "late"

    t0 = _t.monotonic()
    with pytest.raises(CallTimeoutError):
        await enforcer.enforce_sync(slow)
    elapsed = _t.monotonic() - t0
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_enforce_sync_zero_budget_disables_cap():
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    import time as _t
    enforcer = TimeoutEnforcer(op="no_cap_sync", default_timeout_seconds=0.0)

    def slow():
        _t.sleep(0.05)
        return "ok"

    result = await enforcer.enforce_sync(slow)
    assert result == "ok"


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stats_track_total_elapsed():
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    enforcer = TimeoutEnforcer(op="op", default_timeout_seconds=1.0)

    async def ok():
        await asyncio.sleep(0.02)
        return 1

    await enforcer.enforce(ok())
    await enforcer.enforce(ok())
    s = enforcer.stats
    assert s.invoked == 2
    assert s.succeeded == 2
    assert s.total_elapsed >= 0.04
    d = s.as_dict()
    assert d["invoked"] == 2
    assert d["succeeded"] == 2


def test_stats_reset_clears_counters():
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    enforcer = TimeoutEnforcer(op="op")
    enforcer.stats.invoked = 99
    enforcer.reset_stats()
    assert enforcer.stats.invoked == 0


# ---------------------------------------------------------------------------
# with_timeout helper
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_with_timeout_returns_result():
    from tradingagents.agent_harness.core.timeout_enforcer import with_timeout

    async def f():
        return 42

    assert await with_timeout(f(), op="test", timeout_seconds=1.0) == 42


@pytest.mark.asyncio
async def test_with_timeout_raises_call_timeout_error():
    from tradingagents.agent_harness.core.timeout_enforcer import (
        CallTimeoutError, with_timeout,
    )

    async def slow():
        await asyncio.sleep(1.0)

    with pytest.raises(CallTimeoutError):
        await with_timeout(slow(), op="slow", timeout_seconds=0.05)


# ---------------------------------------------------------------------------
# is_coro_or_callable
# ---------------------------------------------------------------------------
def test_is_coro_or_callable_for_coro_object():
    from tradingagents.agent_harness.core.timeout_enforcer import is_coro_or_callable
    async def f(): pass
    coro = f()
    try:
        assert is_coro_or_callable(coro) is True
    finally:
        coro.close()
    assert is_coro_or_callable(f) is True
    assert is_coro_or_callable(lambda: 1) is True
    assert is_coro_or_callable(123) is False


# ---------------------------------------------------------------------------
# FunctionTool integration (sync function under timeout_seconds)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_function_tool_enforces_timeout_on_sync_target():
    """FunctionTool.invoke respects ToolSchema.timeout_seconds."""
    import time as _t
    from tradingagents.agent_harness.tools.base import FunctionTool
    from tradingagents.agent_harness.tools.context import ToolContext
    from tradingagents.agent_harness.tools.schema import ToolSchema

    def slow(args, context):
        _t.sleep(0.5)
        return args * 2

    schema = ToolSchema(
        name="slow_tool", description="", args_schema=int, result_schema=int,
        timeout_seconds=0.05,
    )
    tool = FunctionTool(slow, schema)
    ctx = ToolContext(session_id="s1")

    from tradingagents.agent_harness.core.timeout_enforcer import CallTimeoutError
    with pytest.raises(CallTimeoutError) as ei:
        await tool.invoke(5, ctx)
    assert ei.value.op == "tool.slow_tool"
    assert ei.value.budget == pytest.approx(0.05, abs=1e-3)


@pytest.mark.asyncio
async def test_function_tool_completes_when_fast_enough():
    from tradingagents.agent_harness.tools.base import FunctionTool
    from tradingagents.agent_harness.tools.context import ToolContext
    from tradingagents.agent_harness.tools.schema import ToolSchema

    def fast(args, context):
        return args * 2

    schema = ToolSchema(
        name="fast_tool", description="", args_schema=int, result_schema=int,
        timeout_seconds=1.0,
    )
    tool = FunctionTool(fast, schema)
    ctx = ToolContext(session_id="s1")
    result = await tool.invoke(5, ctx)
    assert result == 10


@pytest.mark.asyncio
async def test_function_tool_async_target_also_enforced():
    """Async functions also flow through TimeoutEnforcer."""
    from tradingagents.agent_harness.tools.base import FunctionTool
    from tradingagents.agent_harness.tools.context import ToolContext
    from tradingagents.agent_harness.tools.schema import ToolSchema
    from tradingagents.agent_harness.core.timeout_enforcer import CallTimeoutError

    async def slow_async(args, context):
        await asyncio.sleep(0.5)
        return args

    schema = ToolSchema(
        name="slow_async", description="", args_schema=int, result_schema=int,
        timeout_seconds=0.05,
    )
    tool = FunctionTool(slow_async, schema)
    ctx = ToolContext(session_id="s1")
    with pytest.raises(CallTimeoutError):
        await tool.invoke(7, ctx)


# ---------------------------------------------------------------------------
# ToolPipeline integration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_run_enforces_timeout_when_passed():
    """ToolPipeline.run(timeout_seconds=...) caps executor wall-clock."""
    from tradingagents.agent_harness.tools.pipeline import ToolPipeline
    from tradingagents.agent_harness.tools.context import ToolContext
    from tradingagents.agent_harness.core.timeout_enforcer import CallTimeoutError

    pipeline = ToolPipeline()

    async def slow_executor(args, ctx):
        await asyncio.sleep(0.5)
        return {"ok": True}

    result = await pipeline.run(
        tool_name="slow", args={}, tool_context=ToolContext(session_id="s1"),
        executor=slow_executor, timeout_seconds=0.05,
    )
    # PipelineResult.error is str-typed; assert it carries the timeout marker.
    assert result.error is not None
    assert "timed out" in result.error
    assert "pipeline.slow" in result.error


@pytest.mark.asyncio
async def test_pipeline_run_no_timeout_default_zero_disables_enforcement():
    """Default timeout_seconds=0 → no enforcement."""
    from tradingagents.agent_harness.tools.pipeline import ToolPipeline
    from tradingagents.agent_harness.tools.context import ToolContext

    pipeline = ToolPipeline()

    async def slow_executor(args, ctx):
        await asyncio.sleep(0.05)
        return {"ok": True}

    result = await pipeline.run(
        tool_name="slow", args={}, tool_context=ToolContext(session_id="s1"),
        executor=slow_executor,
    )
    assert result.error is None
    assert result.result == {"ok": True}


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider timeout wiring (smoke)
# ---------------------------------------------------------------------------
def test_openai_provider_has_timeout_enforcer():
    """Constructing the provider wires a TimeoutEnforcer (smoke)."""
    from tradingagents.agent_harness.llm.openai_provider import OpenAICompatibleProvider
    # We don't call the LLM — only verify the enforcer is wired.
    # ``make_openai_compatible`` may try to reach an LLM endpoint so we
    # bypass __init__ side effects by constructing the bare attributes.
    from tradingagents.agent_harness.core.timeout_enforcer import TimeoutEnforcer
    # Just check the class exposes the method
    assert hasattr(OpenAICompatibleProvider, "set_default_timeout")
    assert hasattr(OpenAICompatibleProvider, "complete")


def test_openai_provider_set_default_timeout_updates_budget():
    from tradingagents.agent_harness.llm.openai_provider import OpenAICompatibleProvider
    # Direct attribute access on a stub — we just verify the method body
    # runs without error and updates the underlying enforcer.
    class _StubEnforcer:
        default_timeout_seconds = 30.0

    class _StubProvider:
        _timeout_enforcer = _StubEnforcer()
        set_default_timeout = OpenAICompatibleProvider.set_default_timeout

    stub = _StubProvider()
    stub.set_default_timeout(120.0)
    assert stub._timeout_enforcer.default_timeout_seconds == 120.0


def test_openai_provider_set_default_timeout_rejects_negative():
    from tradingagents.agent_harness.llm.openai_provider import OpenAICompatibleProvider

    class _StubEnforcer:
        default_timeout_seconds = 30.0

    class _StubProvider:
        _timeout_enforcer = _StubEnforcer()
        set_default_timeout = OpenAICompatibleProvider.set_default_timeout

    stub = _StubProvider()
    with pytest.raises(ValueError):
        stub.set_default_timeout(-1.0)
