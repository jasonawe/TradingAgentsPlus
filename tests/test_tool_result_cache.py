"""§7.3 #4 — Tool result cache (in-memory TTL + LRU).

The harness wraps every :class:`FunctionTool.invoke` with a
:class:`ToolResultCache` lookup so repeated UI polls / planning calls
within the TTL window are free of provider round-trips.  This module
verifies:

- ``make_key`` is stable across processes and orders args canonically.
- ``get`` returns ``None`` on miss and on TTL expiry (auto-evicts).
- ``set`` honours ``ttl_seconds <= 0`` (no cache write).
- LRU eviction kicks in at ``max_entries``.
- ``FunctionTool.invoke`` integrates the cache transparently:
    * cache miss → function runs once
    * cache hit within TTL → function NOT called again
    * different args → cache miss (new key)
    * ``cache_ttl_seconds=0`` → cache disabled entirely
    * ``context.tool_cache`` overrides the process default
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from pydantic import BaseModel

from tradingagents.agent_harness.data.cache import (
    ToolResultCache,
    get_default_cache,
    reset_default_cache,
)
from tradingagents.agent_harness.tools.base import FunctionTool
from tradingagents.agent_harness.tools.context import ToolContext
from tradingagents.agent_harness.tools.schema import ToolSchema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class _QuoteArgs(BaseModel):
    symbol: str
    asset_type: str = "stock"


class _QuoteResult(BaseModel):
    symbol: str
    price: float


@pytest.fixture(autouse=True)
def _isolated_default_cache():
    """Reset the process-wide singleton between tests."""
    reset_default_cache()
    yield
    reset_default_cache()


@pytest.fixture
def ctx() -> ToolContext:
    # Each test gets a fresh cache so the per-tool cache assertions
    # stay hermetic.  We also reset the process-wide default cache.
    return ToolContext(session_id="test-session", tool_cache=ToolResultCache())


# ---------------------------------------------------------------------------
# make_key
# ---------------------------------------------------------------------------
def test_make_key_distinguishes_tool_name():
    a = ToolResultCache.make_key("get_quote", {"symbol": "X"})
    b = ToolResultCache.make_key("get_history", {"symbol": "X"})
    assert a != b
    assert a.startswith("get_quote:")
    assert b.startswith("get_history:")


def test_make_key_is_stable_for_same_args():
    args = _QuoteArgs(symbol="600036.SS")
    a = ToolResultCache.make_key("get_quote", args)
    b = ToolResultCache.make_key("get_quote", args)
    assert a == b


def test_make_key_is_stable_for_dict_with_reordered_keys():
    a = ToolResultCache.make_key("get_quote", {"a": 1, "b": 2})
    b = ToolResultCache.make_key("get_quote", {"b": 2, "a": 1})
    assert a == b


def test_make_key_distinguishes_args():
    a = ToolResultCache.make_key("get_quote", _QuoteArgs(symbol="AAPL"))
    b = ToolResultCache.make_key("get_quote", _QuoteArgs(symbol="MSFT"))
    assert a != b


def test_make_key_handles_none_args():
    k = ToolResultCache.make_key("ping", None)
    assert isinstance(k, str) and k.startswith("ping:")


def test_make_key_falls_back_to_repr_for_arbitrary_objects():
    class _Opaque:
        def __repr__(self) -> str:
            return "opaque-marker"

    a = ToolResultCache.make_key("tool", _Opaque())
    b = ToolResultCache.make_key("tool", _Opaque())
    # Stable + distinct from non-repr cases.
    assert a == b
    assert isinstance(a, str) and a.startswith("tool:")
    assert a != ToolResultCache.make_key("tool", _Opaque.__repr__(_Opaque()))


# ---------------------------------------------------------------------------
# get / set semantics
# ---------------------------------------------------------------------------
def test_set_then_get_returns_value():
    cache = ToolResultCache()
    cache.set("k", {"v": 1}, ttl_seconds=60)
    assert cache.get("k") == {"v": 1}


def test_get_miss_returns_none():
    assert ToolResultCache().get("nope") is None


def test_set_with_zero_ttl_does_not_store():
    cache = ToolResultCache()
    cache.set("k", "v", ttl_seconds=0)
    cache.set("k", "v", ttl_seconds=-1.0)
    assert cache.get("k") is None
    assert len(cache) == 0


def test_get_expired_returns_none_and_evicts():
    fake_now = [1000.0]

    def now() -> float:
        return fake_now[0]

    cache = ToolResultCache(time_source=now)
    cache.set("k", "v", ttl_seconds=5)
    fake_now[0] = 1006.0  # 1s past expiry
    assert cache.get("k") is None
    assert "k" not in cache._entries  # type: ignore[attr-defined]


def test_ttl_boundary_is_inclusive_at_expiry():
    fake_now = [1000.0]
    cache = ToolResultCache(time_source=lambda: fake_now[0])
    cache.set("k", "v", ttl_seconds=5)
    fake_now[0] = 1005.0  # exactly at expiry
    assert cache.get("k") == "v"  # still valid


def test_lru_eviction_drops_oldest_entry():
    cache = ToolResultCache(max_entries=2)
    cache.set("a", 1, ttl_seconds=60)
    cache.set("b", 2, ttl_seconds=60)
    cache.set("c", 3, ttl_seconds=60)
    assert len(cache) == 2
    assert cache.get("a") is None  # evicted
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_lru_touch_promotes_recently_read_entry():
    cache = ToolResultCache(max_entries=2)
    cache.set("a", 1, ttl_seconds=60)
    cache.set("b", 2, ttl_seconds=60)
    _ = cache.get("a")  # touch 'a'
    cache.set("c", 3, ttl_seconds=60)  # forces eviction
    assert cache.get("a") == 1  # 'a' survives because of touch
    assert cache.get("b") is None  # 'b' was the oldest


def test_max_entries_validation():
    with pytest.raises(ValueError):
        ToolResultCache(max_entries=0)
    with pytest.raises(ValueError):
        ToolResultCache(max_entries=-5)


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------
def test_invalidate_specific_key():
    cache = ToolResultCache()
    cache.set("a", 1, ttl_seconds=60)
    cache.set("b", 2, ttl_seconds=60)
    assert cache.invalidate("a") == 1
    assert cache.get("a") is None
    assert cache.get("b") == 2


def test_invalidate_specific_key_missing_returns_zero():
    assert ToolResultCache().invalidate("nope") == 0


def test_invalidate_all_clears_everything():
    cache = ToolResultCache()
    cache.set("a", 1, ttl_seconds=60)
    cache.set("b", 2, ttl_seconds=60)
    assert cache.invalidate() == 2
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------
def test_get_default_cache_returns_singleton():
    a = get_default_cache()
    b = get_default_cache()
    assert a is b


def test_reset_default_cache_replaces_instance():
    a = get_default_cache()
    reset_default_cache()
    b = get_default_cache()
    assert a is not b


def test_stats_reports_size_and_max():
    cache = ToolResultCache(max_entries=42)
    cache.set("a", 1, ttl_seconds=60)
    assert cache.stats() == {"size": 1, "max": 42}


# ---------------------------------------------------------------------------
# FunctionTool integration
# ---------------------------------------------------------------------------
def _make_tool(call_counter: dict, *, ttl: int, is_coro: bool = True):
    """Build a tiny FunctionTool + bump call_counter on every invocation."""
    schema = ToolSchema(
        name="probe",
        description="probe tool for cache tests",
        args_schema=_QuoteArgs,
        result_schema=_QuoteResult,
        cache_ttl_seconds=ttl,
    )

    if is_coro:

        async def _impl(args: _QuoteArgs) -> _QuoteResult:
            call_counter["n"] += 1
            return _QuoteResult(symbol=args.symbol, price=1.0)

        return FunctionTool(_impl, schema)

    def _impl(args: _QuoteArgs) -> _QuoteResult:
        call_counter["n"] += 1
        return _QuoteResult(symbol=args.symbol, price=1.0)

    return FunctionTool(_impl, schema)


@pytest.mark.asyncio
async def test_function_tool_caches_result_for_repeated_args(ctx):
    counter = {"n": 0}
    tool = _make_tool(counter, ttl=60)
    args1 = _QuoteArgs(symbol="AAPL")
    args2 = _QuoteArgs(symbol="AAPL")

    await tool.invoke(args1, ctx)
    await tool.invoke(args2, ctx)
    await tool.invoke(args2, ctx)

    assert counter["n"] == 1  # only the first call ran the underlying impl


@pytest.mark.asyncio
async def test_function_tool_cache_misses_on_different_args(ctx):
    counter = {"n": 0}
    tool = _make_tool(counter, ttl=60)

    await tool.invoke(_QuoteArgs(symbol="AAPL"), ctx)
    await tool.invoke(_QuoteArgs(symbol="MSFT"), ctx)

    assert counter["n"] == 2  # different keys, both miss


@pytest.mark.asyncio
async def test_function_tool_cache_disabled_when_ttl_zero(ctx):
    counter = {"n": 0}
    tool = _make_tool(counter, ttl=0)
    args = _QuoteArgs(symbol="AAPL")

    await tool.invoke(args, ctx)
    await tool.invoke(args, ctx)

    assert counter["n"] == 2  # cache disabled, every call hits impl


@pytest.mark.asyncio
async def test_function_tool_uses_context_cache_over_default():
    """Passing a fresh ToolContext.tool_cache isolates the call."""
    counter = {"n": 0}
    tool = _make_tool(counter, ttl=60)
    args = _QuoteArgs(symbol="AAPL")

    iso_cache = ToolResultCache()
    ctx_iso = ToolContext(session_id="iso", tool_cache=iso_cache)

    await tool.invoke(args, ctx_iso)
    await tool.invoke(args, ctx_iso)

    # isolated cache only saw one call
    assert len(iso_cache) == 1
    # default cache (other ctx) was untouched
    assert len(get_default_cache()) == 0
    assert counter["n"] == 1  # only one invocation


@pytest.mark.asyncio
async def test_function_tool_ttl_expiry_invokes_impl_again():
    """With a fake clock, a cache hit turns into a miss after TTL."""
    counter = {"n": 0}
    fake_now = [1000.0]
    cache = ToolResultCache(time_source=lambda: fake_now[0])

    schema = ToolSchema(
        name="probe",
        description="probe",
        args_schema=_QuoteArgs,
        result_schema=_QuoteResult,
        cache_ttl_seconds=2,
    )

    async def _impl(args: _QuoteArgs) -> _QuoteResult:
        counter["n"] += 1
        return _QuoteResult(symbol=args.symbol, price=1.0)

    tool = FunctionTool(_impl, schema)
    ctx = ToolContext(session_id="s", tool_cache=cache)
    args = _QuoteArgs(symbol="AAPL")

    await tool.invoke(args, ctx)
    assert counter["n"] == 1
    fake_now[0] = 1001.0  # within TTL
    await tool.invoke(args, ctx)
    assert counter["n"] == 1  # still cached
    fake_now[0] = 1003.0  # past TTL
    await tool.invoke(args, ctx)
    assert counter["n"] == 2  # cache evicted, impl ran again


@pytest.mark.asyncio
async def test_function_tool_caches_sync_impl_too(ctx):
    """Sync function paths also use the cache."""
    counter = {"n": 0}
    tool = _make_tool(counter, ttl=60, is_coro=False)
    args = _QuoteArgs(symbol="AAPL")

    await tool.invoke(args, ctx)
    await tool.invoke(args, ctx)

    assert counter["n"] == 1


@pytest.mark.asyncio
async def test_function_tool_cache_hits_return_exact_value(ctx):
    counter = {"n": 0}
    schema = ToolSchema(
        name="probe",
        description="probe",
        args_schema=_QuoteArgs,
        result_schema=_QuoteResult,
        cache_ttl_seconds=60,
    )

    counter_box = {"i": 0}

    async def _impl(args: _QuoteArgs) -> _QuoteResult:
        counter_box["i"] += 1
        return _QuoteResult(symbol=args.symbol, price=counter_box["i"])

    tool = FunctionTool(_impl, schema)
    ctx = ToolContext(session_id="s", tool_cache=ToolResultCache())
    args = _QuoteArgs(symbol="AAPL")

    first = await tool.invoke(args, ctx)
    second = await tool.invoke(args, ctx)

    assert first.price == 1.0
    assert second.price == 1.0  # cached value, not the new counter
