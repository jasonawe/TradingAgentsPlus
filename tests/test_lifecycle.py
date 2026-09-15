"""Q6 / P2-9 — LifecycleHooks。

覆盖:
  - register / unregister / deregister callable
  - register 校验未知 hook 名 / 非 callable
  - fire 同步 callback
  - fire 异步 callback
  - fire exception 隔离
  - fire deny 短路
  - fire 多种 return 形态(dict / HookResult / None)
  - pre_tool_use / post_tool_use / session_start / turn_start 全部 hook 点
  - count / callbacks / 线程安全
"""
from __future__ import annotations

import asyncio

import pytest


def test_register_and_unregister():
    from tradingagents.agent_harness.core.lifecycle import LifecycleHooks
    h = LifecycleHooks()
    h.register("session_start", lambda c: None)
    assert h.count("session_start") == 1
    assert h.unregister("session_start", h.callbacks("session_start")[0]) is True
    assert h.count("session_start") == 0


def test_register_returns_deregister_callable():
    from tradingagents.agent_harness.core.lifecycle import LifecycleHooks
    h = LifecycleHooks()
    dereg = h.register("turn_end", lambda c: None)
    assert h.count("turn_end") == 1
    dereg()
    assert h.count("turn_end") == 0
    # idempotent
    dereg()
    assert h.count("turn_end") == 0


def test_register_validation():
    from tradingagents.agent_harness.core.lifecycle import LifecycleHooks
    h = LifecycleHooks()
    with pytest.raises(ValueError, match="unknown hook"):
        h.register("bogus_event", lambda c: None)
    with pytest.raises(TypeError, match="callable"):
        h.register("session_start", "not-callable")


def test_fire_sync_callback():
    from tradingagents.agent_harness.core.lifecycle import (
        LifecycleHooks, HookContext,
    )
    h = LifecycleHooks()
    captured = []

    def cb(ctx):
        captured.append((ctx.name, ctx.session_id, ctx.tool_name))

    h.register("pre_tool_use", cb)
    ctx = HookContext(name="pre_tool_use", session_id="s1", tool_name="get_quote", args={"symbol": "X"})
    results = asyncio.run(h.fire("pre_tool_use", ctx))
    assert len(results) == 1
    assert results[0].ok
    assert captured == [("pre_tool_use", "s1", "get_quote")]


def test_fire_async_callback():
    from tradingagents.agent_harness.core.lifecycle import (
        LifecycleHooks, HookContext,
    )
    h = LifecycleHooks()
    captured = []

    async def cb(ctx):
        await asyncio.sleep(0)
        captured.append(ctx.tool_name)

    h.register("post_tool_use", cb)
    ctx = HookContext(name="post_tool_use", session_id="s", tool_name="get_quote", result={"price": 1.0})
    asyncio.run(h.fire("post_tool_use", ctx))
    assert captured == ["get_quote"]


def test_fire_callback_exception_isolated():
    from tradingagents.agent_harness.core.lifecycle import (
        LifecycleHooks, HookContext,
    )
    h = LifecycleHooks()

    def bad(ctx):
        raise RuntimeError("boom")

    captured = []

    def good(ctx):
        captured.append("ok")

    h.register("session_start", bad)
    h.register("session_start", good)
    results = asyncio.run(h.fire("session_start", HookContext(name="session_start")))
    assert len(results) == 2
    assert results[0].error is not None
    assert results[1].ok is True
    assert captured == ["ok"]


def test_fire_deny_short_circuits():
    from tradingagents.agent_harness.core.lifecycle import (
        LifecycleHooks, HookContext, any_deny,
    )
    h = LifecycleHooks()
    captured = []

    def first(ctx):
        captured.append("first")

    def deny(ctx):
        captured.append("deny")
        return {"deny": True, "reason": "test"}

    def skipped(ctx):
        captured.append("skipped")

    h.register("pre_tool_use", first)
    h.register("pre_tool_use", deny)
    h.register("pre_tool_use", skipped)

    results = asyncio.run(h.fire("pre_tool_use", HookContext(name="pre_tool_use")))
    assert captured == ["first", "deny"]
    assert any_deny(results) is True


def test_fire_hookresult_return():
    from tradingagents.agent_harness.core.lifecycle import (
        LifecycleHooks, HookContext, HookResult,
    )
    h = LifecycleHooks()

    def cb(ctx):
        return HookResult(callback=cb, deny=False, modified={"x": 1})

    h.register("pre_tool_use", cb)
    results = asyncio.run(h.fire("pre_tool_use", HookContext(name="pre_tool_use")))
    assert results[0].modified == {"x": 1}


def test_fire_unknown_hook_no_callbacks_returns_empty():
    from tradingagents.agent_harness.core.lifecycle import (
        LifecycleHooks, HookContext,
    )
    h = LifecycleHooks()
    results = asyncio.run(h.fire("session_end", HookContext(name="session_end")))
    assert results == []


def test_count_per_hook_and_total():
    from tradingagents.agent_harness.core.lifecycle import LifecycleHooks
    h = LifecycleHooks()
    h.register("session_start", lambda c: None)
    h.register("session_start", lambda c: None)
    h.register("turn_end", lambda c: None)
    h.register("pre_tool_use", lambda c: None)
    assert h.count("session_start") == 2
    assert h.count("turn_end") == 1
    assert h.count("pre_tool_use") == 1
    assert h.count() == 4


def test_callbacks_snapshot_isolated_from_later_changes():
    from tradingagents.agent_harness.core.lifecycle import LifecycleHooks
    h = LifecycleHooks()

    def cb(ctx):
        pass

    h.register("pre_tool_use", cb)
    snap = h.callbacks("pre_tool_use")
    assert snap == [cb]
    # mutate after snapshot
    def cb2(ctx):
        pass

    h.register("pre_tool_use", cb2)
    assert snap == [cb]  # unchanged


def test_all_eight_hook_points_supported():
    from tradingagents.agent_harness.core.lifecycle import LifecycleHooks, ALL_HOOKS
    h = LifecycleHooks()
    for hook in ALL_HOOKS:
        h.register(hook, lambda c: None)
    assert h.count() == 8
    for hook in ALL_HOOKS:
        assert h.count(hook) == 1
