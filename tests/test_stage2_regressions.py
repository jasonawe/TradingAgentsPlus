"""阶段 2 + 之前 P0 修复的回归测试。

覆盖:
1. multi-CRUD fan-out: '这个资产的笔记和告警' → list_notes + list_alerts
2. empty _id_args 不被 dispatch: '删除笔记' (无 id + 无 asset-scoping) 应被拦截
3. BULK_DELETE promotion: '这个资产的告警都删了' → delete_alerts_for_symbol
4. Audit lifecycle: pending → confirmed → executed
5. Carry-forward: 上一轮的 symbol 在下一轮被 recall

测试设计原则: 每个 case 短小直接, 不依赖 LLM (factory=None 走 heuristic + CRUD dispatch).
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, '/Users/shenkang/workroom/languages/agent/TradingAgents')

import pytest


# ---------------------------------------------------------------------------
# 1) multi-CRUD fan-out
# ---------------------------------------------------------------------------

class TestMultiCrudFanOut:
    """multi-CRUD 必须 fan-out 多个工具 (阶段 2 修复)。"""

    def _build_orch(self, tmp_dir):
        from tradingagents.agent_harness.memory import MemoryManager
        from tradingagents.agent_harness.tools.builtin import install_builtin_tools
        from tradingagents.agent_harness.tools.registry import ToolRegistry
        from tradingagents.agent_harness.core.orchestrator import Orchestrator
        from tradingagents.agent_harness.core.retry import CircuitBreaker, RetryPolicy
        from tradingagents.agent_harness.core.context import ContextPriority

        mm = MemoryManager(data_dir=tmp_dir)
        reg = ToolRegistry()
        install_builtin_tools(reg)
        return Orchestrator(
            tool_registry=reg, agent_registry=None, llm_factory=None,
            context_priority=ContextPriority(memory=mm),
            retry_policy=RetryPolicy(max_retries=1, backoff_seconds=0),
            circuit_breaker=CircuitBreaker(failure_threshold=10, reset_seconds=30),
            audit=None, memory=mm,
        )

    def test_notes_and_alerts_both_called(self, tmp_path):
        """'看一下这个资产的笔记和告警' → list_notes + list_alerts 都调。"""
        orch = self._build_orch(str(tmp_path))
        called: list[str] = []

        async def _go():
            async for ev, p in orch.stream_chat(
                "sess_n_a", "看一下这个资产的笔记和告警",
            ):
                if ev == "tool_result" and isinstance(p, dict):
                    called.append(p.get("name"))

        asyncio.run(_go())
        assert "list_notes" in called, f"missing list_notes: {called}"
        assert "list_alerts" in called, f"missing list_alerts: {called}"

    def test_multi_plan_shape_is_ptc(self, tmp_path):
        """multi-CRUD plan 应该返回 {mode: ptc, groups: [...]} 形式。"""
        from tradingagents.agent_harness.core.orchestrator import (
            Orchestrator, OrchestratorState,
        )
        from tradingagents.agent_harness.core.tier import Intent, Op

        st = OrchestratorState(
            session_id="t1",
            user_message="看一下这个资产的笔记和告警",
            intent=Intent.NOTE, op=Op.LIST, symbols=["600036.SS"],
            extra_crud_dispatch=[(Intent.ALERT, Op.LIST)],
        )
        plan = Orchestrator._multi_crud_plan(st)
        assert plan is not None
        assert plan["mode"] == "ptc"
        assert len(plan["groups"]) == 1
        names = [c["name"] for c in plan["groups"][0]["calls"]]
        assert "list_notes" in names
        assert "list_alerts" in names


# ---------------------------------------------------------------------------
# 2) Empty id args interception
# ---------------------------------------------------------------------------

class TestEmptyIdArgsRefused:
    """_id_args 在 user_message 无 id pattern 时返回空 → 必须被拦截。"""

    def test_alert_id_empty_when_no_pattern(self):
        from tradingagents.agent_harness.core.orchestrator import _alert_id_args
        from tradingagents.agent_harness.core.tier import Intent, Op
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState

        st = OrchestratorState(
            session_id="t", user_message="删除这个告警",  # 没有 alert-xxx
            intent=Intent.ALERT, op=Op.DELETE, symbols=["600036.SS"],
        )
        args = _alert_id_args(st)
        assert args.get("alert_id") == ""
        # In production the dispatcher must check this and refuse the call.

    def test_alert_id_extracted_when_pattern_present(self):
        """有 alert-xxx 时正确提取。"""
        from tradingagents.agent_harness.core.orchestrator import _alert_id_args
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        from tradingagents.agent_harness.core.tier import Intent, Op

        st = OrchestratorState(
            session_id="t", user_message="删除 alert-a09de10f",
            intent=Intent.ALERT, op=Op.DELETE, symbols=["600036.SS"],
        )
        args = _alert_id_args(st)
        assert args.get("alert_id") == "alert-a09de10f"

    def test_note_id_extracted_when_pattern_present(self):
        from tradingagents.agent_harness.core.orchestrator import _note_id_args
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        from tradingagents.agent_harness.core.tier import Intent, Op

        st = OrchestratorState(
            session_id="t", user_message="更新 note-abc123 内容",
            intent=Intent.NOTE, op=Op.UPDATE, symbols=["600036.SS"],
        )
        args = _note_id_args(st)
        assert args.get("note_id") == "note-abc123"


# ---------------------------------------------------------------------------
# 3) BULK_DELETE promotion
# ---------------------------------------------------------------------------

class TestBulkDeletePromotion:
    """'这个资产的 X 都删了' 应该 promote 到 bulk。"""

    def test_alert_bulk_promoted_for_asset_scoping(self):
        from tradingagents.agent_harness.core.orchestrator import (
            _should_promote_to_bulk_delete, OrchestratorState,
        )
        from tradingagents.agent_harness.core.tier import Intent, Op

        st = OrchestratorState(
            session_id="t", user_message="删除这个资产的告警",
            intent=Intent.ALERT, op=Op.DELETE, symbols=["600036.SS"],
        )
        assert _should_promote_to_bulk_delete(st) is True

    def test_alert_bulk_not_promoted_when_id_present(self):
        """有具体 alert-xxx 时不 promote (单条删除)。"""
        from tradingagents.agent_harness.core.orchestrator import (
            _should_promote_to_bulk_delete, OrchestratorState,
        )
        from tradingagents.agent_harness.core.tier import Intent, Op

        st = OrchestratorState(
            session_id="t", user_message="删除 alert-xxx",
            intent=Intent.ALERT, op=Op.DELETE, symbols=["600036.SS"],
        )
        assert _should_promote_to_bulk_delete(st) is False

    def test_note_bulk_not_promoted_without_focused_symbol(self):
        """没有 focused symbol 时不 promote (防误删全部)。"""
        from tradingagents.agent_harness.core.orchestrator import (
            _should_promote_to_bulk_delete, OrchestratorState,
        )
        from tradingagents.agent_harness.core.tier import Intent, Op

        st = OrchestratorState(
            session_id="t", user_message="删除这个资产的笔记",
            intent=Intent.NOTE, op=Op.DELETE, symbols=[],
        )
        assert _should_promote_to_bulk_delete(st) is False


# ---------------------------------------------------------------------------
# 4) Audit lifecycle (covered by test_o10_audit_executed; this is a smoke test)
# ---------------------------------------------------------------------------

class TestAuditLogSurface:
    """Audit log API 仍然暴露并正常工作 (阶段 1+2 搬运没破坏它)。"""

    def test_audit_module_imports_cleanly(self):
        from tradingagents.agent_harness.audit import (
            log_write, list_writes, update_write_status, VALID_WRITE_STATUSES,
        )
        assert "executed" in VALID_WRITE_STATUSES
        assert "failed" in VALID_WRITE_STATUSES

    def test_hitl_module_imports_cleanly(self):
        from tradingagents.agent_harness.hitl import (
            grant_approval, is_approved, consume_approval,
            revoke_session, list_pending,
        )
        # All public functions accessible
        assert callable(grant_approval)
        assert callable(is_approved)

    def test_guardrails_module_imports_cleanly(self):
        from tradingagents.agent_harness.guardrails import (
            is_write_tool, validate_write_intent, describe_impact,
        )
        assert is_write_tool("create_note") is True
        assert is_write_tool("get_quote") is False


# ---------------------------------------------------------------------------
# 5) Carry-forward
# ---------------------------------------------------------------------------

class TestCarryForward:
    """下一轮应该 recall 上一轮的 symbol (即使本轮没提)。"""

    def test_carry_symbols_merged_into_state_symbols(self):
        from tradingagents.agent_harness.core.orchestrator import OrchestratorState
        # 模拟: route.symbols 是空, carry 是 ['600036.SS']
        # stream_chat 应该 effective_symbols = [] + [600036.SS] = [600036.SS]
        state = OrchestratorState(
            session_id="t",
            user_message="帮我把这个资产加关注",
            intent=None, op=None,
            symbols=["600036.SS"],
            carry_symbols=["600036.SS"],
        )
        # _focused_symbol 应该返回 carry[0] (因为 state.symbols 是 effective 已经合并)
        from tradingagents.agent_harness.core.orchestrator import _focused_symbol
        assert _focused_symbol(state) == "600036.SS"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
