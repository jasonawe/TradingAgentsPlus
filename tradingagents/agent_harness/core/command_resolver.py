"""Task 7 — CommandResolver.

Spec §26.1:CommandResolver 是 ``CommandSpec`` 的唯一构造者,按
``(entity, op)`` 查表并使用类型化 args factory。ToolExecutor 再以 tool
schema 校验 args;两层任一失败都不会执行。只读 command 直接执行;
写 command 生成 SYSTEM_COMMAND task。

不复制 regex — 复用 tier.py 的 classify / extract_slots / extract_symbols。
不调用 tool / LLM provider。
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .tier import Intent, Op
from ..runtime.models import CommandSpec
from ..tools.permission import PermissionType

LOGGER = logging.getLogger(__name__)


class UnsupportedCommand(ValueError):
    """``(entity, op)`` 不在 CommandResolver 注册表中。"""


# ---------------------------------------------------------------------------
# Args factories — 从 OrchestratorState 派生 tool args
# ---------------------------------------------------------------------------

def _focused_symbol(state) -> str:
    """``state.symbols[0]`` > ``state.carry_symbols[0]`` > ``""``.

    Accepts either a ``dict`` (legacy tests) or an ``OrchestratorState``
    dataclass — both expose ``symbols`` / ``carry_symbols`` either as a
    dict key or as a dataclass field.
    """
    syms = list(_state_attr(state, "symbols") or [])
    if not syms:
        syms = list(_state_attr(state, "carry_symbols") or [])
    return syms[0] if syms else ""


def _state_attr(state, name: str):
    """Read a field from either a dict or a dataclass."""
    if isinstance(state, dict):
        return state.get(name)
    return getattr(state, name, None)


def _watchlist_crud_args(state: dict) -> dict[str, Any]:
    syms = list(state.get("symbols") or [])
    if not syms:
        return {"symbol": "", "asset_type": "stock"}
    return {"symbol": syms[0], "asset_type": "stock"}


def _list_notes_args(state: dict) -> dict[str, Any]:
    args: dict[str, Any] = {}
    sym = _focused_symbol(state)
    if sym:
        args["symbol"] = sym
    if "limit" in state.get("slots", {}):
        args["limit"] = state["slots"]["limit"]
    return args


def _list_alerts_args(state: dict) -> dict[str, Any]:
    sym = _focused_symbol(state)
    args: dict[str, Any] = {"symbol": sym} if sym else {}
    if "limit" in state.get("slots", {}):
        args["limit"] = state["slots"]["limit"]
    return args


def _list_reports_args(state: dict) -> dict[str, Any]:
    sym = _focused_symbol(state)
    args: dict[str, Any] = {"symbol": sym} if sym else {}
    if "limit" in state.get("slots", {}):
        args["limit"] = state["slots"]["limit"]
    return args


def _list_runs_args(state: dict) -> dict[str, Any]:
    sym = _focused_symbol(state)
    args: dict[str, Any] = {"symbol": sym} if sym else {}
    if "limit" in state.get("slots", {}):
        args["limit"] = state["slots"]["limit"]
    return args


def _list_scheduled_tasks_args(state: dict) -> dict[str, Any]:
    sym = _focused_symbol(state)
    args: dict[str, Any] = {"symbol": sym} if sym else {}
    if "limit" in state.get("slots", {}):
        args["limit"] = state["slots"]["limit"]
    return args


def _note_create_args(state: dict) -> dict[str, Any]:
    msg = state.get("user_message") or ""
    slots = state.get("slots") or {}
    body = slots.get("body_md") or msg
    scope = slots.get("scope", "user")
    return {
        "symbol": _focused_symbol(state),
        "body_md": body,
        "asset_type": "stock",
        "scope": scope,
    }


def _note_id_args(state: dict) -> dict[str, Any]:
    msg = state.get("user_message") or ""
    m = re.search(r"note-[A-Za-z0-9_-]+", msg)
    return {"note_id": m.group(0) if m else ""}


def _alert_create_args(state: dict) -> dict[str, Any]:
    slots = state.get("slots") or {}
    direction = slots.get("direction")
    threshold = slots.get("threshold")
    if direction in ("above", "below") and threshold is not None:
        kind = "price_above" if direction == "above" else "price_below"
        params = {"threshold": threshold, "direction": direction}
    else:
        kind = "price"
        params = {"threshold": 0.0}
    scope = slots.get("scope", "user")
    return {
        "symbol": _focused_symbol(state),
        "kind": kind,
        "params": params,
        "asset_type": "stock",
        "scope": scope,
    }


def _alert_id_args(state: dict) -> dict[str, Any]:
    msg = state.get("user_message") or ""
    m = re.search(r"alert-[A-Za-z0-9_-]+", msg)
    return {"alert_id": m.group(0) if m else ""}


def _alert_bulk_delete_args(state: dict) -> dict[str, Any]:
    syms = list(state.get("symbols") or [])
    if not syms:
        syms = list(state.get("carry_symbols") or [])
    return {"symbol": syms[0] if syms else "", "asset_type": "stock"}


def _bulk_by_symbol_args(state: dict) -> dict[str, Any]:
    return {"symbol": _focused_symbol(state), "asset_type": "stock"}


def _scheduled_create_args(state: dict) -> dict[str, Any]:
    slots = state.get("slots") or {}
    cron = slots.get("cron") or "0 9 * * 1-5"
    scope = slots.get("scope", "user")
    return {
        "symbol": _focused_symbol(state),
        "asset_type": "stock",
        "cron_expression": cron,
        "timezone": "Asia/Shanghai",
        "scope": scope,
    }


def _scheduled_id_args(state: dict) -> dict[str, Any]:
    msg = state.get("user_message") or ""
    m = re.search(r"job-[A-Za-z0-9_-]+", msg)
    return {"job_id": m.group(0) if m else ""}


def _run_create_args(state) -> dict[str, Any]:
    import sys
    print(f"[DBG2] _run_create_args called state_type={type(state).__name__}", file=sys.stderr, flush=True)
    slots = _state_attr(state, "slots") or {}
    args = {
        "symbol": _focused_symbol(state),
        "trade_date": slots.get("trade_date", ""),
        "asset_type": "stock",
        "research_depth": slots.get("research_depth", 1),
    }
    # §P3-5 — when the user asked for re-analysis (基于之前的报告 /
    # 再分析 / re-look), pull the prior report id out of the message
    # or fall back to the latest report for the symbol. The runner
    # then injects that report's signal / rating / summary into every
    # analyst prompt and writes a "对比前次" delta section to the
    # new report. Without this, the call still works but the new run
    # wouldn't carry the prior context — defeating the whole feature.
    prior_id = _resolve_prior_report_id(state)
    import sys
    print(f"[DBG2] _run_create_args prior_id={prior_id}", file=sys.stderr, flush=True)
    if prior_id:
        args["based_on_report_id"] = prior_id
    return args


def _resolve_prior_report_id(state) -> str | None:
    """§P3-5 — best-effort resolution of the prior report id for a
    re-analysis request.

    Resolution order:
      1. Explicit ``run-`` or ``report-`` token in the user message.
      2. ``state.slots["based_on_report_id"]`` (caller-supplied).
      3. Most recent report for the focused symbol (falls back to
         global latest when no symbol is anchored).

    Accepts both ``OrchestratorState`` (dataclass-like, has .symbols /
    .user_message / .slots) and a plain dict — the harness test
    harness passes OrchestratorState; the command_resolver pytest tests
    feed dicts.
    """
    msg = getattr(state, "user_message", None) or state.get("user_message") or ""
    m = re.search(r"(?:run|report)-[A-Za-z0-9_-]+", msg)
    if m:
        return m.group(0)
    slots = getattr(state, "slots", None)
    if slots is None:
        slots = state.get("slots") or {}
    if isinstance(slots, dict) and slots.get("based_on_report_id"):
        return str(slots["based_on_report_id"])
    try:
        from tradingagents.agent_harness.tools.impl import _get_report_history
        history = _get_report_history()
    except Exception:
        return None
    if history is None:
        return None
    try:
        records = history.list_reports() or []
    except Exception:
        return None
    if not records:
        return None
    target = (_focused_symbol(state) or "").strip().upper()
    if target:
        for r in records:
            if str(r.get("ticker", "")).upper() == target:
                return r.get("report_id")
    return records[0].get("report_id")


def _run_id_args(state: dict) -> dict[str, Any]:
    msg = state.get("user_message") or ""
    m = re.search(r"run-[A-Za-z0-9_-]+", msg)
    return {"run_id": m.group(0) if m else ""}


def _report_id_args(state: dict) -> dict[str, Any]:
    msg = state.get("user_message") or ""
    m = re.search(r"report-[A-Za-z0-9_-]+", msg)
    return {"report_id": m.group(0) if m else ""}


# ---------------------------------------------------------------------------
# Dispatch table — single source of truth
# ---------------------------------------------------------------------------

_DISPATCH: dict[tuple[Intent, Op], tuple[str, Callable[[dict], dict[str, Any]], PermissionType, str]] = {
    # WATCHLIST
    (Intent.WATCHLIST, Op.LIST): ("list_watchlist", lambda s: {}, PermissionType.READ, "watchlist_panel"),
    (Intent.WATCHLIST, Op.READ): ("list_watchlist", lambda s: {}, PermissionType.READ, "watchlist_panel"),
    (Intent.WATCHLIST, Op.CREATE): ("add_to_watchlist", _watchlist_crud_args, PermissionType.WRITE, "watchlist_panel"),
    (Intent.WATCHLIST, Op.DELETE): ("remove_from_watchlist", _watchlist_crud_args, PermissionType.WRITE, "watchlist_panel"),
    # NOTE
    (Intent.NOTE, Op.LIST): ("list_notes", _list_notes_args, PermissionType.READ, "note_panel"),
    (Intent.NOTE, Op.READ): ("list_notes", _list_notes_args, PermissionType.READ, "note_panel"),
    (Intent.NOTE, Op.CREATE): ("create_note", _note_create_args, PermissionType.WRITE, "note_panel"),
    (Intent.NOTE, Op.UPDATE): ("update_note", _note_id_args, PermissionType.WRITE, "note_panel"),
    (Intent.NOTE, Op.DELETE): ("delete_note", _note_id_args, PermissionType.WRITE, "note_panel"),
    (Intent.NOTE, Op.BULK_DELETE): ("delete_notes_for_symbol", _bulk_by_symbol_args, PermissionType.WRITE, "note_panel"),
    # ALERT
    (Intent.ALERT, Op.LIST): ("list_alerts", _list_alerts_args, PermissionType.READ, "alert_panel"),
    (Intent.ALERT, Op.READ): ("list_alerts", _list_alerts_args, PermissionType.READ, "alert_panel"),
    (Intent.ALERT, Op.CREATE): ("create_alert", _alert_create_args, PermissionType.WRITE, "alert_panel"),
    (Intent.ALERT, Op.UPDATE): ("update_alert", _alert_id_args, PermissionType.WRITE, "alert_panel"),
    (Intent.ALERT, Op.DELETE): ("delete_alert", _alert_id_args, PermissionType.WRITE, "alert_panel"),
    (Intent.ALERT, Op.BULK_DELETE): ("delete_alerts_for_symbol", _alert_bulk_delete_args, PermissionType.WRITE, "alert_panel"),
    # SCHEDULED
    (Intent.SCHEDULED, Op.LIST): ("list_scheduled_tasks", _list_scheduled_tasks_args, PermissionType.READ, "scheduled_panel"),
    (Intent.SCHEDULED, Op.READ): ("list_scheduled_tasks", _list_scheduled_tasks_args, PermissionType.READ, "scheduled_panel"),
    (Intent.SCHEDULED, Op.CREATE): ("create_scheduled_task", _scheduled_create_args, PermissionType.WRITE, "scheduled_panel"),
    (Intent.SCHEDULED, Op.UPDATE): ("update_scheduled_task", _scheduled_id_args, PermissionType.WRITE, "scheduled_panel"),
    (Intent.SCHEDULED, Op.DELETE): ("delete_scheduled_task", _scheduled_id_args, PermissionType.WRITE, "scheduled_panel"),
    (Intent.SCHEDULED, Op.BULK_DELETE): ("delete_scheduled_tasks_for_symbol", _bulk_by_symbol_args, PermissionType.WRITE, "scheduled_panel"),
    (Intent.SCHEDULED, Op.RUN): ("run_scheduled_task", _scheduled_id_args, PermissionType.WRITE, "scheduled_panel"),
    # RUN (analysis)
    (Intent.RUN, Op.LIST): ("list_runs", _list_runs_args, PermissionType.READ, "run_panel"),
    (Intent.RUN, Op.READ): ("get_analysis_status", _run_id_args, PermissionType.READ, "run_panel"),
    (Intent.RUN, Op.CREATE): ("run_trading_agents_analysis", _run_create_args, PermissionType.WORKFLOW, "run_panel"),
    (Intent.RUN, Op.DELETE): ("cancel_analysis_run", _run_id_args, PermissionType.WRITE, "run_panel"),
    # REPORT
    (Intent.REPORT, Op.LIST): ("list_reports", _list_reports_args, PermissionType.READ, "report_panel"),
    (Intent.REPORT, Op.READ): ("get_report", _report_id_args, PermissionType.READ, "report_panel"),
}


@dataclass
class CommandResolver:
    """``CommandSpec`` 的唯一构造者。"""

    # 暴露给 orchestrator 的别名(向后兼容):Orchestrator._CRUD_DISPATCH 现在直接
    # 引用这里的 _DISPATCH。
    DISPATCH: dict[tuple[Intent, Op], tuple] = field(default_factory=lambda: _DISPATCH)

    def resolve(
        self,
        *,
        intent: Intent,
        op: Op,
        user_message: str,
        symbols: list[str] | None = None,
        carry_symbols: list[str] | None = None,
        slots: dict | None = None,
    ) -> CommandSpec:
        """按 ``(intent, op)`` 查表,返回 :class:`CommandSpec`。

        Raises:
            UnsupportedCommand: 不在注册表中的 ``(intent, op)`` 组合。
        """
        key = (intent, op)
        spec_tuple = self.DISPATCH.get(key)
        if spec_tuple is None:
            raise UnsupportedCommand(
                f"no command spec registered for (intent={intent.value}, op={op.value})"
            )
        tool_name, args_factory, permission, result_view = spec_tuple
        # 用 dict 而不是 OrchestratorState — 解耦
        state = {
            "user_message": user_message,
            "symbols": list(symbols or []),
            "carry_symbols": list(carry_symbols or []),
            "slots": dict(slots or {}),
        }
        args = args_factory(state)
        requires_approval = permission in (PermissionType.WRITE, PermissionType.WORKFLOW)
        return CommandSpec(
            command_id=str(uuid.uuid4()),
            entity=_intent_to_entity(intent),
            op=_op_to_op(op),
            tool_name=tool_name,
            args=args,
            permission=permission,
            requires_approval=requires_approval,
            result_view=result_view,
        )


def _intent_to_entity(intent: Intent) -> str:
    """Intent → CommandSpec.entity 映射。"""
    return {
        Intent.WATCHLIST: "watchlist",
        Intent.NOTE: "note",
        Intent.ALERT: "alert",
        Intent.SCHEDULED: "scheduled",
        Intent.RUN: "run",
        Intent.REPORT: "report",
    }.get(intent, "unknown")


def _op_to_op(op: Op) -> str:
    """Op → CommandSpec.op 映射。"""
    return op.value.upper() if op else "READ"


__all__ = [
    "CommandResolver",
    "UnsupportedCommand",
    "_DISPATCH",
]
