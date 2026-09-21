"""§P3-3 — Entity × Op CRUD dispatch table.

Coverage:
  1. classify() — entity × op classification (8 cases).
  2. Orchestrator._CRUD_DISPATCH — every (entity, op) pair maps to the right tool
     (22 entries).
  3. _crud_plan_for_state() — orchestrator returns the right one-step
     plan for each entity × op.
  4. fast_route_with_op() — routing returns the right tier.
  5. End-to-end smoke: WATCHLIST_DELETE via the orchestrator pipeline.

Design constraint (per user): **no special cases**. Every CRUD entity
goes through ``Orchestrator._CRUD_DISPATCH``; adding a new entity requires only
1 enum value + 1 keyword set + 1 dispatch line.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock as MM

import pytest

from tradingagents.agent_harness.core.tier import (
    Intent,
    Op,
    classify,
    classify_intent,
    fast_route,
    fast_route_with_op,
)
from tradingagents.agent_harness.core.orchestrator import (
    Orchestrator,
    OrchestratorState,
)


# ---------------------------------------------------------------------------
# 1. classify() — entity × op classification
# ---------------------------------------------------------------------------

class TestClassifyEntityOp:
    """Every user message should map to (entity, op) the dispatch table
    can act on.  Eight canonical cases covering all CRUD entities and
    all ops we expose."""

    @pytest.mark.parametrize("msg, expected_intent, expected_op", [
        # WATCHLIST — no UPDATE (we don't support reorder via op)
        ("我的关注列表", Intent.WATCHLIST, Op.LIST),
        ("看看我的关注", Intent.WATCHLIST, Op.LIST),
        ("加入关注 600036.SS", Intent.WATCHLIST, Op.CREATE),
        ("把这个资产从我的关注中删除", Intent.WATCHLIST, Op.DELETE),
        # NOTE
        ("我的笔记", Intent.NOTE, Op.LIST),
        ("新建笔记 关于招商银行", Intent.NOTE, Op.CREATE),
        ("更新笔记 note-abc", Intent.NOTE, Op.UPDATE),
        ("删除笔记 note-abc", Intent.NOTE, Op.DELETE),
        # ALERT
        ("我的告警", Intent.ALERT, Op.LIST),
        ("新建告警 600036 涨幅超过 5%", Intent.ALERT, Op.CREATE),
        ("修改告警 alert-abc", Intent.ALERT, Op.UPDATE),
        ("删除告警 alert-abc", Intent.ALERT, Op.DELETE),
        # SCHEDULED — incl. RUN
        ("我的定时任务", Intent.SCHEDULED, Op.LIST),
        ("新建定时任务", Intent.SCHEDULED, Op.CREATE),
        ("修改定时任务 job-xyz", Intent.SCHEDULED, Op.UPDATE),
        ("删除定时任务 job-xyz", Intent.SCHEDULED, Op.DELETE),
        ("立即触发定时任务 job-xyz", Intent.SCHEDULED, Op.RUN),
        # RUN (analysis)
        ("分析任务列表", Intent.RUN, Op.LIST),
        ("跑一下 600036.SS", Intent.RUN, Op.CREATE),
        # REPORT
        ("分析报告", Intent.REPORT, Op.LIST),
    ])
    def test_classify(self, msg, expected_intent, expected_op):
        intent, op = classify(msg)
        assert intent == expected_intent, f"{msg!r}: {intent} != {expected_intent}"
        assert op == expected_op, f"{msg!r}: {op} != {expected_op}"

    def test_classify_fallback_to_legacy_intent(self):
        """No CRUD keyword → legacy read-only intent (QUOTE/NEWS/...) with Op.READ."""
        intent, op = classify("招商银行 600036 现在多少钱")
        assert intent == Intent.QUOTE
        assert op == Op.READ


# ---------------------------------------------------------------------------
# 2. Orchestrator._CRUD_DISPATCH — coverage of all (entity, op) pairs
# ---------------------------------------------------------------------------

# Build the expected matrix from the spec: every (entity, op) we promise
# must be present. Adding a new entity = add 1 line here + 1 in tier.
EXPECTED_DISPATCH = {
    (Intent.WATCHLIST, Op.LIST):   "list_watchlist",
    (Intent.WATCHLIST, Op.READ):   "list_watchlist",
    (Intent.WATCHLIST, Op.CREATE): "add_to_watchlist",
    (Intent.WATCHLIST, Op.DELETE): "remove_from_watchlist",
    (Intent.NOTE, Op.LIST):        "list_notes",
    (Intent.NOTE, Op.READ):        "list_notes",
    (Intent.NOTE, Op.CREATE):      "create_note",
    (Intent.NOTE, Op.UPDATE):      "update_note",
    (Intent.NOTE, Op.DELETE):      "delete_note",
    (Intent.ALERT, Op.LIST):       "list_alerts",
    (Intent.ALERT, Op.READ):       "list_alerts",
    (Intent.ALERT, Op.CREATE):     "create_alert",
    (Intent.ALERT, Op.UPDATE):     "update_alert",
    (Intent.ALERT, Op.DELETE):     "delete_alert",
    (Intent.ALERT, Op.BULK_DELETE): "delete_alerts_for_symbol",
    (Intent.NOTE, Op.BULK_DELETE):  "delete_notes_for_symbol",
    (Intent.SCHEDULED, Op.BULK_DELETE): "delete_scheduled_tasks_for_symbol",
    (Intent.SCHEDULED, Op.LIST):   "list_scheduled_tasks",
    (Intent.SCHEDULED, Op.READ):   "list_scheduled_tasks",
    (Intent.SCHEDULED, Op.CREATE): "create_scheduled_task",
    (Intent.SCHEDULED, Op.UPDATE): "update_scheduled_task",
    (Intent.SCHEDULED, Op.DELETE): "delete_scheduled_task",
    (Intent.SCHEDULED, Op.RUN):    "run_scheduled_task",
    (Intent.RUN, Op.LIST):         "list_runs",
    (Intent.RUN, Op.READ):         "get_analysis_status",
    (Intent.RUN, Op.CREATE):       "run_trading_agents_analysis",
    (Intent.RUN, Op.DELETE):       "cancel_analysis_run",
    (Intent.REPORT, Op.LIST):      "list_reports",
    (Intent.REPORT, Op.READ):      "get_report",
}


class TestCrudDispatchTable:
    """The dispatch table is the single source of truth for what
    (entity, op) maps to what tool name. Lock down its membership +
    tool-name mapping so refactors can't silently break coverage."""

    def test_all_expected_pairs_present(self):
        for key, tool_name in EXPECTED_DISPATCH.items():
            assert key in Orchestrator._CRUD_DISPATCH, f"missing dispatch entry for {key}"
            spec = Orchestrator._CRUD_DISPATCH[key]
            assert spec[0] == tool_name, (
                f"{key}: dispatch tool {spec[0]!r} != expected {tool_name!r}"
            )

    def test_no_extra_pairs(self):
        """If a (entity, op) is added, the test class above will fail
        (expected list updated), this catches accidental additions."""
        assert set(Orchestrator._CRUD_DISPATCH.keys()) == set(EXPECTED_DISPATCH.keys()), (
            f"dispatch table has unexpected keys: "
            f"{set(Orchestrator._CRUD_DISPATCH.keys()) - set(EXPECTED_DISPATCH.keys())}"
        )

    def test_total_entries_count(self):
        """Sanity: we promise exactly N pairs. If you add/remove one,
        update both this test and EXPECTED_DISPATCH."""
        assert len(Orchestrator._CRUD_DISPATCH) == 29  # 27 + NOTE.BULK_DELETE + SCHEDULED.BULK_DELETE


# ---------------------------------------------------------------------------
# 3. _crud_plan_for_state() — orchestrator integration
# ---------------------------------------------------------------------------

class TestCrudPlanForState:
    """The orchestrator should turn ``(intent, op)`` into a one-step
    plan via ``_crud_plan_for_state``. We construct minimal
    OrchestratorState objects and call the helper directly — no LLM
    wiring required (the helper is pure dict construction)."""

    def _state(self, intent: Intent, op: Op, user_message: str = "x",
               symbols: list | None = None) -> OrchestratorState:
        return OrchestratorState(
            session_id="test-session",
            user_message=user_message,
            intent=intent,
            symbols=list(symbols or []),
            op=op,
        )

    def test_watchlist_create_with_symbol(self):
        st = self._state(Intent.WATCHLIST, Op.CREATE, "加入关注 600036",
                         symbols=["600036.SS"])
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan == [{"step": 1, "action": "add_to_watchlist",
                         "args": {"symbol": "600036.SS", "asset_type": "stock"}}]

    def test_watchlist_delete_with_carry_symbol(self):
        """The state.symbols list may already be carry-forward from a
        previous turn (see §P3-2). The args factory just picks symbols[0]."""
        st = self._state(Intent.WATCHLIST, Op.DELETE, "从我的关注中删除",
                         symbols=["600036.SS"])
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan[0]["action"] == "remove_from_watchlist"
        assert plan[0]["args"]["symbol"] == "600036.SS"

    def test_note_update_extracts_note_id(self):
        st = self._state(Intent.NOTE, Op.UPDATE, "更新笔记 note-abc123 内容")
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan[0]["action"] == "update_note"
        assert plan[0]["args"]["note_id"] == "note-abc123"

    def test_alert_create_uses_carry_symbol(self):
        st = self._state(Intent.ALERT, Op.CREATE, "对这个建告警",
                         symbols=["600036.SS"])
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan[0]["action"] == "create_alert"
        assert plan[0]["args"]["symbol"] == "600036.SS"

    def test_scheduled_run_extracts_job_id(self):
        st = self._state(Intent.SCHEDULED, Op.RUN, "立即触发定时任务 job-xyz")
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan[0]["action"] == "run_scheduled_task"
        assert plan[0]["args"]["job_id"] == "job-xyz"

    def test_run_create_uses_carry_symbol(self):
        st = self._state(Intent.RUN, Op.CREATE, "跑一下 600036.SS",
                         symbols=["600036.SS"])
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan[0]["action"] == "run_trading_agents_analysis"
        assert plan[0]["args"]["symbol"] == "600036.SS"

    def test_run_delete_extracts_run_id(self):
        st = self._state(Intent.RUN, Op.DELETE, "取消 run-runid123")
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan[0]["action"] == "cancel_analysis_run"
        assert plan[0]["args"]["run_id"] == "run-runid123"

    def test_report_list_returns_empty_args(self):
        st = self._state(Intent.REPORT, Op.LIST, "我的报告")
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan[0]["action"] == "list_reports"
        assert plan[0]["args"] == {}

    def test_unknown_pair_returns_empty_plan(self):
        """(WATCHLIST, UPDATE) is not in the table — must fall through
        cleanly so the orchestrator can use its data-only heuristic."""
        st = self._state(Intent.WATCHLIST, Op.UPDATE, "改关注")
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan == []

    def test_no_op_returns_empty_plan(self):
        st = self._state(Intent.WATCHLIST, None, "watchlist no op")
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan == []


# ---------------------------------------------------------------------------
# 4. fast_route_with_op() — tier routing for CRUD
# ---------------------------------------------------------------------------

class TestFastRouteWithOp:
    """fast_route_with_op() must return the right (RouteResult, Op) for
    CRUD entities. READ/LIST → Tier 1 DIRECT; CREATE/UPDATE/DELETE →
    Tier 2 PLAN_EXECUTE."""

    @pytest.mark.parametrize("msg, expected_tier", [
        ("我的关注", 1),      # LIST  → Tier 1
        ("我的笔记", 1),      # LIST  → Tier 1
        ("我的告警", 1),      # LIST  → Tier 1
        ("定时任务", 1),      # LIST  → Tier 1
        ("我的报告", 1),      # LIST  → Tier 1
    ])
    def test_list_routes_to_tier1(self, msg, expected_tier):
        from tradingagents.agent_harness.core.tier import Tier
        route, op = fast_route_with_op(msg)
        assert route.tier == Tier.DIRECT
        assert op == Op.LIST

    @pytest.mark.parametrize("msg, expected_op", [
        ("加入关注 600036", Op.CREATE),
        ("删除关注 600036", Op.DELETE),
        ("新建告警", Op.CREATE),
        ("删除笔记 note-x", Op.DELETE),
        ("新建定时任务", Op.CREATE),
        ("立即触发定时任务 job-x", Op.RUN),
        ("跑一下 600036", Op.CREATE),
    ])
    def test_write_routes_to_tier2_with_correct_op(self, msg, expected_op):
        from tradingagents.agent_harness.core.tier import Tier
        route, op = fast_route_with_op(msg)
        assert op == expected_op
        assert route.tier == Tier.PLAN_EXECUTE


# ---------------------------------------------------------------------------
# 5. End-to-end: WATCHLIST_DELETE via real DB
# ---------------------------------------------------------------------------

class TestEndToEndWatchlistDelete:
    """Smoke test: classify → crud_plan → execute a real
    remove_from_watchlist against a tmp SQLite store. Verifies the
    end-to-end wiring (orchestrator → tool_registry → repo)."""

    @pytest.fixture
    def repo(self, tmp_path):
        from web.storage import SQLiteStore
        from web.repositories import WatchlistRepository
        return WatchlistRepository(SQLiteStore(tmp_path / "settings.db"))

    @pytest.fixture
    def injected_repo(self, repo):
        from tradingagents.agent_harness.tools import impl as tools_bridge
        prev = getattr(tools_bridge, "_repos", {})
        tools_bridge._repos = {**prev, "watchlist": repo}
        yield repo
        tools_bridge._repos = prev

    @pytest.mark.asyncio
    async def test_classify_plan_dispatch(self, injected_repo):
        """Verify the full chain for "从我的关注中删除 600036.SS":
        classify → entity=WATCHLIST, op=DELETE
        crud_plan → [{action: remove_from_watchlist, args: {symbol: 600036.SS}}]
        """
        from tradingagents.agent_harness.tools.builtin import (
            RemoveFromWatchlistArgs, remove_from_watchlist,
        )
        from tradingagents.agent_harness.tools.context import ToolContext

        # Seed: add 600036.SS first
        await remove_from_watchlist(
            RemoveFromWatchlistArgs(symbol="600036.SS", asset_type="stock"),
            ToolContext(session_id="e2e"),
        )
        # re-add via direct repo call (remove_from_watchlist on empty list = not_found)
        from tradingagents.agent_harness.tools.builtin import (
            AddToWatchlistArgs, add_to_watchlist,
        )
        seed = await add_to_watchlist(
            AddToWatchlistArgs(symbol="600036.SS", asset_type="stock"),
            ToolContext(session_id="e2e"),
        )
        assert seed.status == "created"

        # Now: classify → plan → args
        msg = "从我的关注中删除 600036.SS"
        intent, op = classify(msg)
        assert intent == Intent.WATCHLIST
        assert op == Op.DELETE

        st = OrchestratorState(
            session_id="e2e", user_message=msg,
            intent=intent, symbols=["600036.SS"], op=op,
        )
        plan = Orchestrator._crud_plan_for_state(st)
        assert plan[0]["action"] == "remove_from_watchlist"

        # Execute the plan step directly (no orchestrator needed for
        # the wiring check)
        args = RemoveFromWatchlistArgs(
            symbol=plan[0]["args"]["symbol"],
            asset_type=plan[0]["args"]["asset_type"],
        )
        result = await remove_from_watchlist(args, ToolContext(session_id="e2e"))
        assert result.status == "deleted"
        assert result.symbol == "600036.SS"


# ════════════════════════════════════════════════════════
# Task 7 — CommandResolver (new API)
# ════════════════════════════════════════════════════════

class TestCommandResolver:
    """CommandResolver 是 CommandSpec 的唯一构造者,按 (entity, op) 查表 + 类型化 args factory。"""

    def _resolver(self):
        from tradingagents.agent_harness.core.command_resolver import CommandResolver
        return CommandResolver()

    def test_resolve_watchlist_create_returns_command_spec(self):
        from tradingagents.agent_harness.runtime.models import CommandSpec
        from tradingagents.agent_harness.tools.permission import PermissionType
        spec = self._resolver().resolve(
            intent=Intent.WATCHLIST, op=Op.CREATE,
            user_message="加入关注 600036.SS",
            symbols=["600036.SS"],
        )
        assert isinstance(spec, CommandSpec)
        assert spec.entity == "watchlist"
        assert spec.op == "CREATE"
        assert spec.tool_name == "add_to_watchlist"
        assert spec.args["symbol"] == "600036.SS"
        assert spec.args["asset_type"] == "stock"
        assert spec.permission == PermissionType.WRITE

    def test_resolve_watchlist_list_returns_direct_read_permission(self):
        from tradingagents.agent_harness.tools.permission import PermissionType
        spec = self._resolver().resolve(
            intent=Intent.WATCHLIST, op=Op.LIST,
            user_message="我的关注",
            symbols=[],
        )
        assert spec.permission == PermissionType.READ

    def test_resolve_note_create_uses_slot_body_md(self):
        spec = self._resolver().resolve(
            intent=Intent.NOTE, op=Op.CREATE,
            user_message="给 600036 加一个笔记：哈哈打MVP",
            symbols=["600036.SS"],
            slots={"body_md": "哈哈打MVP"},
        )
        assert spec.tool_name == "create_note"
        assert spec.args["symbol"] == "600036.SS"
        # body 来自 slot,不是整条 message
        assert spec.args["body_md"] == "哈哈打MVP"

    def test_resolve_note_create_fallback_to_message(self):
        spec = self._resolver().resolve(
            intent=Intent.NOTE, op=Op.CREATE,
            user_message="新建笔记 招商银行最近不错",
            symbols=["600036.SS"],
            slots={},
        )
        # slot 没匹配上时 fallback 到 user_message
        assert spec.args["body_md"] == "新建笔记 招商银行最近不错"

    def test_resolve_alert_create_uses_threshold_and_direction_slots(self):
        spec = self._resolver().resolve(
            intent=Intent.ALERT, op=Op.CREATE,
            user_message="建告警 600036 涨幅超过 5%",
            symbols=["600036.SS"],
            slots={"threshold": 5.0, "direction": "above"},
        )
        assert spec.tool_name == "create_alert"
        assert spec.args["kind"] == "price_above"
        assert spec.args["params"]["threshold"] == 5.0
        assert spec.args["params"]["direction"] == "above"

    def test_resolve_scheduled_create_uses_cron_slot(self):
        spec = self._resolver().resolve(
            intent=Intent.SCHEDULED, op=Op.CREATE,
            user_message="每天早上 9 点跑 600036",
            symbols=["600036.SS"],
            slots={"cron": "0 9 * * *"},
        )
        assert spec.tool_name == "create_scheduled_task"
        assert spec.args["cron_expression"] == "0 9 * * *"
        assert spec.args["timezone"] == "Asia/Shanghai"

    def test_resolve_run_create_uses_trade_date_slot(self):
        spec = self._resolver().resolve(
            intent=Intent.RUN, op=Op.CREATE,
            user_message="跑一下 600036 明天",
            symbols=["600036.SS"],
            slots={"trade_date": "2026-09-22"},
        )
        assert spec.tool_name == "run_trading_agents_analysis"
        assert spec.args["trade_date"] == "2026-09-22"
        assert spec.args["research_depth"] == 1

    def test_resolve_note_list_carries_focused_symbol(self):
        spec = self._resolver().resolve(
            intent=Intent.NOTE, op=Op.LIST,
            user_message="看看这个资产的笔记",
            symbols=[], carry_symbols=["600036.SS"],
        )
        assert spec.tool_name == "list_notes"
        assert spec.args.get("symbol") == "600036.SS"

    def test_resolve_note_list_no_focus_returns_empty_args(self):
        spec = self._resolver().resolve(
            intent=Intent.NOTE, op=Op.LIST,
            user_message="我的所有笔记",
            symbols=[], carry_symbols=[],
        )
        assert spec.tool_name == "list_notes"
        # 无 focus 时不应硬塞 symbol
        assert spec.args.get("symbol", "") == ""

    def test_resolve_alert_bulk_delete_uses_focused_symbol(self):
        spec = self._resolver().resolve(
            intent=Intent.ALERT, op=Op.BULK_DELETE,
            user_message="删除这个资产的所有告警",
            symbols=["600036.SS"], carry_symbols=[],
        )
        assert spec.tool_name == "delete_alerts_for_symbol"
        assert spec.args["symbol"] == "600036.SS"

    def test_resolve_unsupported_pair_raises(self):
        from tradingagents.agent_harness.core.command_resolver import UnsupportedCommand
        with pytest.raises(UnsupportedCommand):
            self._resolver().resolve(
                intent=Intent.QUOTE, op=Op.CREATE,  # QUOTE 不支持 CREATE
                user_message="创建报价",
                symbols=["600036.SS"],
            )

    def test_resolve_does_not_invoke_tools_or_llm(self):
        """CommandResolver 只构造 CommandSpec,不能调用 tool 或 LLM。"""
        import inspect
        from tradingagents.agent_harness.core import command_resolver
        src = inspect.getsource(command_resolver)
        for forbidden in ["llm_factory", "LLMFactory", "tool.invoke", "invoke_tool"]:
            assert forbidden not in src, f"CommandResolver 不能引用 {forbidden}"
