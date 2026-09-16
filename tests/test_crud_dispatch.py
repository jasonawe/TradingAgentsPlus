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
        from tradingagents.agents.general import tools_bridge
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
