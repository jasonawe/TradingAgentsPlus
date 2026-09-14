"""P0-3 Tool pipeline 5-stage tests.

Covers:
- Empty pipeline (no hooks) → direct executor pass-through
- pre_execute DENY → returns denied result, executor never called
- pre_execute ASK → returns needs_approval, executor never called
- pre_execute ALLOW → proceeds to execute
- pre_execute first-deny-wins (waterfall semantics)
- monotonic guard DENY → executor never called (fail-closed semantics)
- post_execute REPLACE → result replaced, replaced flag set
- post_execute DENY → returns denied result after execute
- result hook receives final PipelineResult (synchronous)
- DangerousToolGuard matches delete_/cancel_/drop_/clear_/reset_ prefixes
- DangerousToolGuard allows safe tools (get_quote etc.)
- Hook failure modes (pre fail-open, guard fail-closed, post skip)
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from tradingagents.agent_harness.tools.pipeline import (
    ToolPipeline, PipelineContext, PipelineStage, Decision,
    DangerousToolGuard,
)


def _run(coro):
    return asyncio.run(coro)


class _StubExecutor:
    def __init__(self, return_value=None, raise_exc=None):
        self._return = return_value
        self._raise = raise_exc
        self.calls: list[tuple] = []

    async def __call__(self, args, context):
        self.calls.append((args, context))
        if self._raise:
            raise self._raise
        return self._return


# --------------------------------------------------------------------------
# Baseline (no hooks)
# --------------------------------------------------------------------------
class TestEmptyPipeline:
    def test_no_hooks_direct_passthrough(self):
        pipeline = ToolPipeline()
        exe = _StubExecutor(return_value={"price": 41.71})
        result = _run(pipeline.run(
            tool_name="get_quote", args={"symbol": "X"},
            tool_context=MagicMock(), executor=exe,
        ))
        assert result.ok is True
        assert result.result == {"price": 41.71}
        assert result.denied is False
        assert result.needs_approval is False
        assert len(exe.calls) == 1

    def test_executor_exception_becomes_error_result(self):
        pipeline = ToolPipeline()
        exe = _StubExecutor(raise_exc=RuntimeError("kaboom"))
        result = _run(pipeline.run(
            tool_name="get_quote", args={}, tool_context=MagicMock(), executor=exe,
        ))
        assert result.ok is False
        assert "kaboom" in result.error

    def test_hook_count_zero_initially(self):
        pipeline = ToolPipeline()
        assert pipeline.hook_count() == {
            "pre_execute": 0, "guard": 0, "post_execute": 0, "result": 0,
        }


# --------------------------------------------------------------------------
# pre_execute
# --------------------------------------------------------------------------
class TestPreExecute:
    def test_deny_short_circuits_before_execute(self):
        async def deny_all(pctx):
            pctx.deny_reason = "explicit deny"
            return Decision.DENY
        pipeline = ToolPipeline()
        pipeline.add_pre_execute(deny_all)
        exe = _StubExecutor()
        result = _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(), executor=exe,
        ))
        assert result.denied is True
        assert result.denied_by == "deny_all"
        assert result.error == "explicit deny"
        assert exe.calls == [], "executor must NOT be called when denied"

    def test_ask_short_circuits_with_payload(self):
        async def ask_all(pctx):
            pctx.ask_payload = {"foo": "bar"}
            return Decision.ASK
        pipeline = ToolPipeline()
        pipeline.add_pre_execute(ask_all)
        exe = _StubExecutor()
        result = _run(pipeline.run(
            tool_name="x", args={"a": 1}, tool_context=MagicMock(), executor=exe,
        ))
        assert result.needs_approval is True
        assert result.approval_payload == {"foo": "bar"}
        assert exe.calls == [], "executor must NOT be called when awaiting approval"

    def test_first_deny_wins_waterfall(self):
        """When multiple pre_execute hooks run, the first DENY short-circuits."""
        order = []

        async def allow_hook(pctx):
            order.append("allow1")
            return Decision.ALLOW

        async def deny_hook(pctx):
            order.append("deny2")
            return Decision.DENY

        async def should_not_run(pctx):
            order.append("should_not_run")
            return Decision.DENY

        pipeline = ToolPipeline()
        pipeline.add_pre_execute(allow_hook)
        pipeline.add_pre_execute(deny_hook)
        pipeline.add_pre_execute(should_not_run)
        result = _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(),
            executor=_StubExecutor(),
        ))
        assert order == ["allow1", "deny2"]
        assert result.denied is True
        assert result.denied_by == "deny_hook"

    def test_allow_proceeds_to_execute(self):
        async def allow_hook(pctx):
            return Decision.ALLOW
        pipeline = ToolPipeline()
        pipeline.add_pre_execute(allow_hook)
        exe = _StubExecutor(return_value="ok")
        result = _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(), executor=exe,
        ))
        assert result.ok is True
        assert result.result == "ok"
        assert len(exe.calls) == 1

    def test_pre_hook_exception_fails_open(self):
        async def broken_hook(pctx):
            raise RuntimeError("boom")
        async def allow_hook(pctx):
            return Decision.ALLOW
        pipeline = ToolPipeline()
        pipeline.add_pre_execute(broken_hook)
        pipeline.add_pre_execute(allow_hook)
        result = _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(),
            executor=_StubExecutor(return_value="rescued"),
        ))
        assert result.ok is True
        assert result.result == "rescued"


# --------------------------------------------------------------------------
# monotonic guards
# --------------------------------------------------------------------------
class TestGuards:
    def test_guard_blocks_after_pre_execute_allow(self):
        async def allow_hook(pctx):
            return Decision.ALLOW
        async def guard_always_deny(pctx):
            return False
        pipeline = ToolPipeline()
        pipeline.add_pre_execute(allow_hook)
        pipeline.add_guard(guard_always_deny)
        exe = _StubExecutor()
        result = _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(), executor=exe,
        ))
        assert result.denied is True
        assert exe.calls == []

    def test_guard_failure_fails_closed(self):
        """Guards that raise should DENY (fail-closed for safety)."""
        async def guard_raises(pctx):
            raise RuntimeError("oops")
        pipeline = ToolPipeline()
        pipeline.add_guard(guard_raises)
        result = _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(),
            executor=_StubExecutor(),
        ))
        assert result.denied is True

    def test_multiple_guards_all_must_allow(self):
        async def g1(pctx): return True
        async def g2(pctx): return True
        async def g3(pctx): return False
        pipeline = ToolPipeline()
        pipeline.add_guard(g1)
        pipeline.add_guard(g2)
        pipeline.add_guard(g3)
        result = _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(),
            executor=_StubExecutor(),
        ))
        assert result.denied is True


# --------------------------------------------------------------------------
# post_execute
# --------------------------------------------------------------------------
class TestPostExecute:
    def test_replace_swaps_result(self):
        async def replace_hook(pctx):
            pctx.result = {"replaced": True}
            return Decision.REPLACE
        pipeline = ToolPipeline()
        pipeline.add_post_execute(replace_hook)
        result = _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(),
            executor=_StubExecutor(return_value={"original": True}),
        ))
        assert result.ok is True
        assert result.result == {"replaced": True}
        assert result.replaced is True

    def test_deny_after_execute(self):
        """post_execute can deny even after execute succeeds."""
        async def post_deny(pctx):
            return Decision.DENY
        pipeline = ToolPipeline()
        pipeline.add_post_execute(post_deny)
        result = _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(),
            executor=_StubExecutor(return_value="computed"),
        ))
        assert result.denied is True
        # Result not surfaced when post denied
        assert result.result is None

    def test_post_exception_skips_continuation(self):
        async def broken(pctx): raise RuntimeError("bad")
        async def mark_done(pctx):
            pctx.result = "marked"
            return Decision.REPLACE
        pipeline = ToolPipeline()
        pipeline.add_post_execute(broken)
        pipeline.add_post_execute(mark_done)
        result = _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(),
            executor=_StubExecutor(return_value="orig"),
        ))
        assert result.result == "marked"


# --------------------------------------------------------------------------
# result observation
# --------------------------------------------------------------------------
class TestResultHook:
    def test_result_hook_observes_final_outcome(self):
        observed = []
        def observer(pctx, result):
            observed.append((pctx.tool_name, result.ok, result.result))
        pipeline = ToolPipeline()
        pipeline.add_result(observer)
        _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(),
            executor=_StubExecutor(return_value="ok"),
        ))
        assert observed == [("x", True, "ok")]

    def test_result_hook_sees_denied_results(self):
        observed = []
        def observer(pctx, result):
            observed.append((result.denied, result.denied_by))
        async def deny(pctx): return Decision.DENY
        pipeline = ToolPipeline()
        pipeline.add_pre_execute(deny)
        pipeline.add_result(observer)
        _run(pipeline.run(
            tool_name="x", args={}, tool_context=MagicMock(),
            executor=_StubExecutor(),
        ))
        assert observed == [(True, "deny")]


# --------------------------------------------------------------------------
# DangerousToolGuard sample hook
# --------------------------------------------------------------------------
class TestDangerousToolGuard:
    def test_flags_delete(self):
        async def runner():
            guard = DangerousToolGuard()
            pctx = PipelineContext(tool_name="delete_alert", args={"id": 42},
                                   tool_context=MagicMock())
            return await guard(pctx)
        assert _run(runner()) == Decision.ASK

    def test_flags_cancel_scheduled(self):
        async def runner():
            guard = DangerousToolGuard()
            pctx = PipelineContext(tool_name="cancel_scheduled_task", args={},
                                   tool_context=MagicMock())
            return await guard(pctx)
        assert _run(runner()) == Decision.ASK

    def test_flags_drop(self):
        async def runner():
            guard = DangerousToolGuard()
            pctx = PipelineContext(tool_name="drop_watchlist", args={},
                                   tool_context=MagicMock())
            return await guard(pctx)
        assert _run(runner()) == Decision.ASK

    def test_allows_safe_tool(self):
        async def runner():
            guard = DangerousToolGuard()
            pctx = PipelineContext(tool_name="get_quote", args={"symbol": "X"},
                                   tool_context=MagicMock())
            return await guard(pctx)
        assert _run(runner()) == Decision.ALLOW

    def test_ask_payload_populated(self):
        async def runner():
            guard = DangerousToolGuard()
            pctx = PipelineContext(tool_name="delete_alert", args={"id": 42},
                                   tool_context=MagicMock())
            await guard(pctx)
            return pctx.ask_payload
        payload = _run(runner())
        assert payload == {
            "tool": "delete_alert",
            "args": {"id": 42},
            "reason": "destructive_tool_requires_approval",
        }

    def test_extra_patterns(self):
        async def runner():
            guard = DangerousToolGuard(extra_patterns=("purge_",))
            pctx = PipelineContext(tool_name="purge_history", args={},
                                   tool_context=MagicMock())
            return await guard(pctx)
        assert _run(runner()) == Decision.ASK

    def test_full_pipeline_with_dangerous_guard(self):
        """End-to-end: pre_execute DangerousToolGuard → ASK → executor not called."""
        pipeline = ToolPipeline()
        pipeline.add_pre_execute(DangerousToolGuard())
        exe = _StubExecutor()
        result = _run(pipeline.run(
            tool_name="delete_alert", args={"id": 42},
            tool_context=MagicMock(), executor=exe,
        ))
        assert result.needs_approval is True
        assert result.approval_payload["tool"] == "delete_alert"
        assert exe.calls == []
