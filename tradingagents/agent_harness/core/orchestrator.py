
"""Tier 2 orchestrator — 5-node state machine (v2 spec D5, N20 fix).

Top-level nodes: PlanNode → ExecuteNode → ObserveNode → VerifyNode → SynthesizeNode.

The 17+ sub-states from §D5 are represented by the helper methods
``_plan`` / ``_execute_step`` / ``_observe`` / ``_verify`` / ``_synthesize``
plus the verification subgraph (L1/L2 checks), the HITL subgraph
(``_await_confirmation`` for write ops), and the emitter subgraph
(``_emit_final``).

Implemented as a pure-Python state machine (no langgraph dependency)
so the project doesn't add a heavy graph runtime just for Tier 2.

LLM calls: 2 (plan + synthesize); L3 verifier adds 1 more if enabled.
"""
from __future__ import annotations
import time

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

from tradingagents.agent_harness.tools import ToolContext, ToolRegistry
from tradingagents.agent_harness.ptc import PTCExecutor, parse_program
from tradingagents.agent_harness.tools.pipeline import ToolPipeline, DangerousToolGuard

from .context import ContextPriority
from .retry import CircuitBreaker, RetryPolicy, retry_async
from web.market_models import ProviderError
from tradingagents.agent_harness.llm.failure import LlmFailure, LlmFailureKind
from .token_usage import TokenUsageStore, attach_store, track_agent
from .surface import SurfaceRouter
from .session_store import Session, SessionStore
from .harness_checkpoint import (
    ALL_NODES,
    LEGACY_MILESTONE_ID,
    HarnessCheckpoint,
    HarnessCheckpointStore,
    NODE_DONE,
    NODE_EXECUTING,
    NODE_OBSERVING,
    NODE_PLANNING,
    NODE_SYNTHESIZING,
    NODE_VERIFYING,
)
from .short_circuit import ShortCircuit
from .tier import Intent, Op, RouteResult, Tier, fast_route, fast_route_with_op, maybe_degrade_to_tier1
from .verification import VerificationLevel, Verifier

# ════════════════════════════════════════════════════════════════════
# §P3-3+ §7.3 — short-window dedupe for destructive tool calls.
# ════════════════════════════════════════════════════════════════════
# Symptom: LLM 会在一次 chat 里反复 call 同一个 (tool, args) 对 ——
# 第一次 execute 成功(matched=1, deleted=1),第二次又来(matched=0,
# deleted=0),第三次又来触发 approval dialog,UI 看着像"撤销了"。本质
# 是 LLM 的 plan 行为问题,我们在 orchestrator 层加 TTL 60s 的缓存:
# 同一 session 内,同一 (tool, args) 已成功 execute,直接返回上次
# result + 标记 _deduped=True,跳过 approve / invoke / 弹窗。
from collections import OrderedDict

_DEDUPE_WINDOW_SECONDS = 60.0
_DEDUPE_MAX_ENTRIES = 256

_recent_tool_results: "OrderedDict[tuple[str, str, str], tuple[float, dict]]" = OrderedDict()
# asyncio.gather in ``_execute`` runs _run_step concurrently; multiple
# steps can hit _dedupe_record / _dedupe_lookup at the same time on the
# module-level OrderedDict. The OrderedDict itself is not thread-safe
# (CPython protects individual ops by the GIL but the move_to_end +
# popitem pair in _dedupe_record is NOT atomic across threads).
import threading as _threading
_dedupe_lock = _threading.Lock()


def _dedupe_key(name: str, args: Any) -> str:
    """稳定 hash:同 (name, args) → 同 key,不依赖 dict 顺序。"""
    try:
        s = json.dumps(_dump_for_dedupe(args), sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        s = repr(args)
    import hashlib
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _dump_for_dedupe(obj: Any) -> Any:
    """Pydantic / dataclass 友好的序列化。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _dump_for_dedupe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_dump_for_dedupe(v) for v in obj]
    if hasattr(obj, "model_dump"):
        try:
            return _dump_for_dedupe(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return _dump_for_dedupe(vars(obj))
    return str(obj)


def _dedupe_lookup(session_id: str, name: str, args: Any) -> dict | None:
    """60s 内已成功执行过 → 返回 cached result(含 _deduped 标记);否则 None。"""
    import time as _t
    key = (session_id, name, _dedupe_key(name, args))
    with _dedupe_lock:
        cached = _recent_tool_results.get(key)
        if cached is None:
            return None
        ts, result = cached
        if _t.time() - ts > _DEDUPE_WINDOW_SECONDS:
            _recent_tool_results.pop(key, None)
            return None
        # hit → 移到末尾(LRU 语义)
        _recent_tool_results.move_to_end(key)
    return {**result, "_deduped": True, "_cached_age_s": round(_t.time() - ts, 1)}


def _dedupe_record(session_id: str, name: str, args: Any, result: dict) -> None:
    """成功执行后写入缓存;满了就 LRU 驱逐最旧的。"""
    import time as _t
    key = (session_id, name, _dedupe_key(name, args))
    with _dedupe_lock:
        _recent_tool_results[key] = (_t.time(), result)
        _recent_tool_results.move_to_end(key)
        while len(_recent_tool_results) > _DEDUPE_MAX_ENTRIES:
            _recent_tool_results.popitem(last=False)

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Verification helpers (P8 L3)
# ---------------------------------------------------------------------------


def _stringify(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(), ensure_ascii=False, default=str)
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def _worst_verification(*results):
    """Return the strictest (lowest ok, highest level) of the inputs."""
    from .verification import VerificationLevel

    if not results:
        return None
    ok = all(r.ok for r in results)
    max_level = max((r.level for r in results), default=VerificationLevel.L1_STRUCTURAL)
    reasons = [r.reason for r in results if r.reason]
    details = {f"L{i+1}": r.details for i, r in enumerate(results) if r.details}
    from .verification import VerificationResult
    return VerificationResult(
        ok=ok,
        level=max_level,
        reason="; ".join(reasons) if reasons else "ok",
        details=details or None,
    )




@dataclass
class OrchestratorState:
    """Snapshot of an in-flight orchestration."""

    session_id: str
    user_message: str
    intent: Intent = Intent.UNKNOWN
    symbols: list[str] = field(default_factory=list)
    # §P3-2 — symbols carried forward from the previous turn in this
    # session because the current user_message has none. Distinct from
    # ``symbols`` so the planner can choose to use only the explicit ones.
    carry_symbols: list[str] = field(default_factory=list)
    prior_user_msg: str | None = None
    # §Step1+ — hybrid slot filling: structured params pulled out of
    # the user message before routing. Keys: time_range / threshold /
    # direction / limit / cron. ``None`` when the slot wasn't found
    # (orchestrator falls back to LLM synthesis to fill it in).
    slots: dict = field(default_factory=dict)
    # §P3-3 — verb discriminator paired with ``intent``. Set from
    # :func:`tier.classify`. Together they pin the user's CRUD action
    # down to a single (entity, op) cell in the _CRUD_DISPATCH table.
    op: Any | None = None  # tradingagents.agent_harness.core.tier.Op
    # §P3-3+: when ``classify_multi`` detects 2+ CRUD entities in one
    # turn (e.g. "看看告警和笔记" -> [(NOTE,LIST),(ALERT,LIST)]),
    # ``extra_crud_dispatch`` carries the secondary pairs. The primary
    # ``intent``/``op`` stay on the state for backward compat;
    # ``_plan`` reads ``extra_crud_dispatch`` to expand the plan into
    # a multi-step sequence.
    extra_crud_dispatch: list[tuple[Any, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] | dict[str, Any] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final: Any = None
    error: str | None = None
    # §P2 — synth retry on L3 fail: turn-level retry counter + the
    # directive string injected into the next ``_llm_synthesize``
    # call. Set by ``_stream_chat_impl`` after L3 ungrounded; cleared
    # by ``_llm_synthesize`` after it consumes the directive. Capped
    # at ``Orchestrator._SYNTH_TURN_RETRY_LIMIT``.
    synth_retry_count: int = 0
    synth_retry_directive: str | None = None
    # The most recent L3 verdict, set by ``_run_l3_judge`` so the
    # orchestrator loop can decide whether to retry without
    # re-running the LLM judge.
    last_l3: Any = None
    # §7.3 #9 Prefetcher: created by ``_plan`` after the plan is known;
    # ``_execute`` awaits ``drain()`` and consults ``lookup()`` for cache
    # hits so tool calls fired in parallel between plan and execute
    # short-circuit the actual ``tool.invoke``.
    prefetcher: Any = None  # tradingagents.agent_harness.core.prefetch.Prefetcher
    # Q1 / P2-4: live turn + step bookkeeping. Filled in by ``_stream_chat_impl``.
    turn: Any = None
    current_step_id: int = 0
    current_step_started_at: float = 0.0


# ---------------------------------------------------------------------------
# §P3-3 — CRUD args factories
# ---------------------------------------------------------------------------
# These small functions take an OrchestratorState and return the args dict
# for the tool that the _CRUD_DISPATCH table selects. They are module-level
# (not methods) so they can be referenced from the dispatch dict literally
# without binding ``self``.

def _watchlist_crud_args(state: Any) -> dict[str, Any]:
    """watchlist create/delete needs (symbol, asset_type). Use the first
    explicit or carry-forward symbol."""
    syms = list(state.symbols or [])
    if not syms:
        return {"symbol": "", "asset_type": "stock"}
    return {"symbol": syms[0], "asset_type": "stock"}




def _should_promote_to_bulk_delete(state: Any) -> bool:
    """§P3-3+ — decide if a single-record DELETE should be promoted
    to BULK_DELETE because the user said 'this asset's X' without
    a specific ID and a focused symbol is available.

    Returns True when ALL of:
    - op is DELETE
    - intent is a CRUD entity that supports BULK_DELETE
      (NOTE / ALERT / SCHEDULED; RUN is excluded because
      cancel_analysis_run is single-record)
    - The user message has no specific ID pattern (note-xxx /
      alert-xxx / job-xxx)
    - A focused symbol is available (explicit or carry-forward)
    - The message has an asset-scoping phrase ('这个资产的',
      '该资产的', '它的', '此资产的', 'all of this', 'its', etc.)
      OR the message just says "删除 X" with no other content

    Pre-fix, the user had to say '都删' / '全部删除' for bulk to
    fire. Now '删除这个资产的笔记' also routes to bulk when a
    focused symbol is in scope.
    """
    op = getattr(state, "op", None)
    intent = getattr(state, "intent", None)
    if op != Op.DELETE:
        return False
    if intent not in (Intent.NOTE, Intent.ALERT, Intent.SCHEDULED):
        return False
    if not _focused_symbol(state):
        return False
    msg = (getattr(state, "user_message", "") or "").lower()
    # If the message contains a specific ID pattern, the user
    # clearly means a single record — don't promote.
    if re.search(r"(note|alert|job|run)-[a-z0-9_-]+", msg):
        return False
    # Asset-scoping phrases that imply "all of this asset's X"
    asset_scoping = [
        "这个资产的", "该资产的", "此资产的", "它的", "此标的的",
        "这个标的的", "该标的的", "本资产的",
        "for this asset", "for the asset", "all of this",
    ]
    has_scope_phrase = any(p in msg for p in asset_scoping)
    # Also promote if the message is just "删除 X" with no
    # specific identifier — the user clearly means "all of them
    # for this asset".
    short_delete = (
        len(msg) <= 30
        and "删除" in msg
        and not re.search(r"\d", msg)  # no specific number/ID
    )
    return has_scope_phrase or short_delete


def _focused_symbol(state: Any) -> str:
    """§P3-3+ — resolve the focused symbol for read tools.

    Order of preference:
    1. ``state.symbols[0]`` (explicit in the current user message)
    2. ``state.carry_symbols[0]`` (carry-forward from a prior turn)

    Empty string if neither is set so the tool's optional ``symbol``
    field stays ``None`` and the tool returns unfiltered data — the
    case where the user really did ask for "all".
    """
    syms = list(getattr(state, "symbols", []) or [])
    if not syms:
        syms = list(getattr(state, "carry_symbols", []) or [])
    return syms[0] if syms else ""


def _list_notes_args(state: Any) -> dict[str, Any]:
    """list_notes: when the focused symbol is known (current or carry-
    forward), pass it as the filter so the tool returns scoped data.
    When empty, returns {} so the tool returns all notes.

    §Step4 — also pass ``state.slots["limit"]`` when the user said
    "最近 N 条" / "前 N 个" so the tool short-circuits without
    returning the full history. Falls back to no limit when the slot
    is absent (legacy behaviour preserved).
    """
    sym = _focused_symbol(state)
    slots = getattr(state, "slots", {}) or {}
    args: dict[str, Any] = {}
    if sym:
        args["symbol"] = sym
    if "limit" in slots:
        args["limit"] = slots["limit"]
    return args


def _list_alerts_args(state: Any) -> dict[str, Any]:
    """list_alerts: same scoping as ``_list_notes_args``.

    §Step4 — also pass ``state.slots["limit"]`` when set."""
    sym = _focused_symbol(state)
    slots = getattr(state, "slots", {}) or {}
    args: dict[str, Any] = {"symbol": sym} if sym else {}
    if "limit" in slots:
        args["limit"] = slots["limit"]
    return args


def _list_reports_args(state: Any) -> dict[str, Any]:
    """list_reports: scope to the focused symbol when one is known

    §Step4 — also pass ``state.slots["limit"]`` when set.
    (explicit ``state.symbols[0]`` or carry-forward ``state.carry_symbols[0]``).

    Empty args when no focused symbol — preserves "all reports" behaviour.
    """
    sym = _focused_symbol(state)
    slots = getattr(state, "slots", {}) or {}
    args: dict[str, Any] = {"symbol": sym} if sym else {}
    if "limit" in slots:
        args["limit"] = slots["limit"]
    return args


def _list_runs_args(state: Any) -> dict[str, Any]:
    """list_runs: scope to the focused symbol when one is known.

    §Step4 — also pass ``state.slots["limit"]`` when set.

    Same carry-forward semantics as :func:`_list_notes_args`; empty
    args when no focused symbol is set so ``manager.list_runs`` is
    used (unfiltered) instead of ``list_runs_for_ticker``.
    """
    sym = _focused_symbol(state)
    slots = getattr(state, "slots", {}) or {}
    args: dict[str, Any] = {"symbol": sym} if sym else {}
    if "limit" in slots:
        args["limit"] = slots["limit"]
    return args


def _list_scheduled_tasks_args(state: Any) -> dict[str, Any]:
    """list_scheduled_tasks: scope to the focused symbol when one is known.

    §Step4 — also pass ``state.slots["limit"]`` when set.

    Each scheduler job carries a ``symbol`` field; the bridge tool
    filters ``items`` in Python when ``symbol`` is non-empty.
    """
    sym = _focused_symbol(state)
    slots = getattr(state, "slots", {}) or {}
    args: dict[str, Any] = {"symbol": sym} if sym else {}
    if "limit" in slots:
        args["limit"] = slots["limit"]
    return args


def _note_create_args(state: Any) -> dict[str, Any]:
    """§P3-3+ — create_note defaults the symbol to the focused one
    (state.symbols[0] > state.carry_symbols[0]) so '建一个笔记' after
    discussing 600036.SS auto-tags the note for 600036.SS.

    §Step3 — prefer ``state.slots["body_md"]`` over the full
    ``state.user_message`` so '给 600036 加一个笔记：哈哈打MVP'
    stores '哈哈打MVP' (not the whole sentence) as the note body.
    Falls back to user_message when no slot was extracted (regex
    didn't find a colon-separated body in the message).
    """
    msg = state.user_message or ""
    slots = getattr(state, "slots", {}) or {}
    body = slots.get("body_md") or msg
    # §Step 15 — forward scope slot ('user' / 'agent' / 'shared')
    # into the write args. Defaults to 'user' via the schema; user-only
    # writes stay local, shared/agent are routed through different scopes.
    scope = slots.get("scope", "user")
    return {
        "symbol": _focused_symbol(state),
        "body_md": body,
        "asset_type": "stock",
        "scope": scope,
    }


def _note_id_args(state: Any) -> dict[str, Any]:
    """update_note / delete_note: extract the first note_id-looking token
    (``note-xxx`` or just digits) from the user message. Falls back to an
    empty string so the tool reports its own error."""
    import re as _re
    msg = state.user_message or ""
    m = re.search(r"note-[A-Za-z0-9_-]+", msg)
    if m:
        return {"note_id": m.group(0)}
    return {"note_id": ""}


def _alert_create_args(state: Any) -> dict[str, Any]:
    """§P3-3+ — create_alert defaults the symbol to the focused one
    (state.symbols[0] > state.carry_symbols[0]). Pre-fix, only
    state.symbols[0] was used which missed the carry-forward case
    ('建一个 50 块的告警' on the second turn of a 600036 conversation
    would lose the symbol).

    §Step2 — when :func:`extract_slots` populated threshold / direction
    on the state (e.g. "价格超过 50 提醒我"), prefer those over the
    legacy 0.0 default. direction "above" → kind=price_above; "below"
    → kind=price_below. The ``params`` dict carries both fields so
    ``tools_bridge.create_alert``'s _translate_alert_args can match
    either variant.
    """
    slots = getattr(state, "slots", {}) or {}
    direction = slots.get("direction")
    threshold = slots.get("threshold")
    if direction in ("above", "below") and threshold is not None:
        kind = "price_above" if direction == "above" else "price_below"
        params = {"threshold": threshold, "direction": direction}
    else:
        kind = "price"
        params = {"threshold": 0.0}
    # §Step 15 — forward scope slot.
    scope = slots.get("scope", "user")
    return {
        "symbol": _focused_symbol(state),
        "kind": kind,
        "params": params,
        "asset_type": "stock",
        "scope": scope,
    }


def _alert_id_args(state: Any) -> dict[str, Any]:
    import re as _re
    msg = state.user_message or ""
    m = re.search(r"alert-[A-Za-z0-9_-]+", msg)
    if m:
        return {"alert_id": m.group(0)}
    return {"alert_id": ""}


def _alert_bulk_delete_args(state: Any) -> dict[str, Any]:
    """§P3-3+ — args for ``delete_alerts_for_symbol``.

    Prefers ``state.symbols[0]`` (current-turn explicit); falls back
    to ``state.carry_symbols[0]`` (previous-turn anchor). Empty string
    if neither is available so the tool reports its own validation
    error rather than silently deleting nothing.
    """
    syms = list(getattr(state, "symbols", []) or [])
    if not syms:
        syms = list(getattr(state, "carry_symbols", []) or [])
    return {
        "symbol": syms[0] if syms else "",
        "asset_type": "stock",
    }


def _bulk_by_symbol_args(state: Any) -> dict[str, Any]:
    """§P3-3+ — generic (symbol, asset_type) factory for
    ``delete_notes_for_symbol`` and ``delete_scheduled_tasks_for_symbol``.

    Same carry-forward logic as :func:`_alert_bulk_delete_args`.
    """
    return {
        "symbol": _focused_symbol(state),
        "asset_type": "stock",
    }


def _scheduled_create_args(state: Any) -> dict[str, Any]:
    """§P3-3+ — create_scheduled_task defaults the symbol to the
    focused one (state.symbols[0] > state.carry_symbols[0]). Cron
    and timezone have sensible defaults; the LLM plan can override.

    §Step2 — when :func:`extract_slots` populated ``cron`` on the
    state (e.g. "每天早上 9 点跑" → "0 9 * * *"), prefer that over
    the weekdays-09:00 default. Falls back to the default only when
    the slot is absent or failed to parse.
    """
    slots = getattr(state, "slots", {}) or {}
    cron = slots.get("cron") or "0 9 * * 1-5"
    # §Step 15 — forward scope slot.
    scope = slots.get("scope", "user")
    return {
        "symbol": _focused_symbol(state),
        "asset_type": "stock",
        "cron_expression": cron,
        "timezone": "Asia/Shanghai",
        "scope": scope,
    }


def _scheduled_id_args(state: Any) -> dict[str, Any]:
    import re as _re
    msg = state.user_message or ""
    m = re.search(r"job-[A-Za-z0-9_-]+", msg)
    if m:
        return {"job_id": m.group(0)}
    return {"job_id": ""}


def _run_create_args(state: Any) -> dict[str, Any]:
    """§P3-3+ — run_trading_agents_analysis defaults the symbol to
    the focused one (state.symbols[0] > state.carry_symbols[0]).

    §Step3 — read ``trade_date`` and ``research_depth`` from
    :func:`extract_slots` ("今天" / "明天" / "YYYY-MM-DD" / "深度 N").
    Falls back to ``""`` (today) and ``1`` (default depth) when no
    slot was extracted.
    """
    slots = getattr(state, "slots", {}) or {}
    return {
        "symbol": _focused_symbol(state),
        "trade_date": slots.get("trade_date", ""),
        "asset_type": "stock",
        "research_depth": slots.get("research_depth", 1),
    }


def _run_id_args(state: Any) -> dict[str, Any]:
    import re as _re
    msg = state.user_message or ""
    m = re.search(r"run-[A-Za-z0-9_-]+", msg)
    if m:
        return {"run_id": m.group(0)}
    return {"run_id": ""}


def _report_id_args(state: Any) -> dict[str, Any]:
    """§Step 16 — prefer state.slots['report_id'] (extracted by
    ``extract_slots`` from a ``run-<hex>`` token) over the legacy regex.

    Falls back to the legacy ``report-<id>`` regex when the slot is
    absent so callers that bypass extract_slots (e.g. tests) keep
    working.
    """
    import re as _re
    slots = getattr(state, "slots", {}) or {}
    rid = slots.get("report_id")
    if rid:
        return {"report_id": rid}
    msg = state.user_message or ""
    m = re.search(r"report-[A-Za-z0-9_-]+", msg)
    if m:
        return {"report_id": m.group(0)}
    return {"report_id": ""}


class Orchestrator:
    """5-node Tier 2 orchestrator.

    Parameters
    ----------
    tool_registry, llm_factory, context_priority, retry_policy,
    circuit_breaker, audit:
        Shared collaborators (N4/N15 fix — Orchestrator does NOT own the
        tool/agent registries; it queries them lazily).
    """

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        agent_registry: Any,
        llm_factory: Any,
        context_priority: ContextPriority,
        retry_policy: RetryPolicy,
        circuit_breaker: CircuitBreaker,
        audit: Any,
        enable_l3: bool = False,
        judge_factory: Any | None = None,
        checkpoint_store: HarnessCheckpointStore | None = None,
        session_store: SessionStore | None = None,
        memory: Any | None = None,
        # §Step 27 — opt-in persistent plan cache. When provided,
        # plans survive process restarts so the new web process hits
        # the cache instead of replaying the LLM round-trip. Default:
        # in-memory only (backward-compatible).
        plan_cache_db_path: str | None = None,
        plan_cache_ttl_seconds: float = 300.0,
        plan_cache_max_entries: int = 256,
        # §P3-4 Phase 3 — when the harness has per-agent override
        # factories configured, this callable returns the right one
        # for the named agent (planner / synthesizer). Falls back to
        # ``self.llm_factory`` (the main factory) when no override
        # is set, so callers can blindly invoke
        # ``self._llm_factory_for(name)`` without first checking
        # whether the agent has an override.
        llm_factory_for: "Callable[[str], Any] | None" = None,
    ) -> None:
        self.tool_registry = tool_registry
        self.agent_registry = agent_registry
        self.llm_factory = llm_factory
        # §P3-4 Phase 3 — when the harness has per-agent override
        # factories configured, this callable returns the right one
        # for the named agent (planner / synthesizer). It falls back
        # to the main ``llm_factory`` when no override is set, so
        # callers can blindly invoke ``self.llm_factory_for(name)``
        # without first checking whether the agent has an override.
        self._llm_factory_for = llm_factory_for or (lambda _name: llm_factory)
        self.context_priority = context_priority
        self.retry_policy = retry_policy
        self.circuit_breaker = circuit_breaker
        self.audit = audit
        self.enable_l3 = enable_l3
        # §P3-2 — per-turn memory persistence. Without this ref the
        # ChatHistoryLayerProvider sees an empty L1 every turn, the
        # planner has no cross-turn symbol carry-forward, and the LLM
        # responds to "加入关注" with "请补充标的" even when the same
        # session just discussed 600036.SS.
        self.memory = memory

        self._short_circuit = ShortCircuit(tool_registry)
        # PTC executor: same tool registry, concurrent group dispatch
        # Also routes through the same 5-stage pipeline for HITL consistency
        self._tool_pipeline = ToolPipeline()
        self._tool_pipeline.add_pre_execute(DangerousToolGuard())
        self._ptc_executor = PTCExecutor(tool_registry, pipeline=self._tool_pipeline)
        # Verifier gets its own judge_factory (N89 fix: judge model != main LLM)
        self._verifier = Verifier(judge_factory=judge_factory)
        # Surface router (P0-4): tags every event with surface (ui/audit/
        # debug/internal). Debug events go to logger.debug; audit sinks
        # are configurable but defaults to no-op so we don't fight the
        # existing manual audit.log(...) calls in _stream_chat_impl.
        self._surface_router = SurfaceRouter()
        # Crash recovery (P1-7): when wired, every milestone event is
        # persisted so resume() can replay + continue after restart.
        self._checkpoint_store = checkpoint_store
        # Session metadata (roadmap A2): when wired, every stream_chat
        # auto-creates/updates the session row + bumps message_count.
        self._session_store = session_store
        # Q4 / P1-4: AgentControlBus for steer / inject / whenIdle runtime
        # control. Owned by Orchestrator so callers can route commands
        # without poking private state; the bus itself is process-wide
        # (keyed by session_id) so multiple orchestrators share.
        from tradingagents.agent_harness.core.agent_control import AgentControlBus
        self.control_bus: AgentControlBus = AgentControlBus()
        # Q1 / P2-4: TurnBoundary + history. ``turn_boundary`` hands out
        # monotonic ``turn_id`` values; ``turn_history`` keeps the last
        # ``TURN_HISTORY_LIMIT`` :class:`TurnRecord` entries for
        # observability / debugging (not for audit — that lives in
        # EventLog).
        from tradingagents.agent_harness.core.turn_boundary import TurnBoundary
        self.turn_boundary = TurnBoundary()
        self._turn_history: list = []
        self._turn_history_limit: int = 50
        # Q6 / P2-9: LifecycleHooks registry. Plugins / observability
        # code subscribe to session_start / pre_tool_use / etc. via
        # ``harness.orchestrator.lifecycle.register(name, cb)``.
        from tradingagents.agent_harness.core.lifecycle import LifecycleHooks
        self.lifecycle: LifecycleHooks = LifecycleHooks()
        # §7.3 #6: PlanTemplateCache — skip LLM plan when an identical
        # user_message was planned within the last 5 minutes (N65 fix).
        # Defaults: TTL 5min, max 256 entries.
        # §Step 27 — opt-in persistent backing via a SQLite file.
        # When ``plan_cache_db_path`` is provided, plans survive
        # process restarts so a fresh web process can hit the cache
        # instead of replaying the LLM round-trip. Default: in-memory
        # only (backward-compatible).
        from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
        if plan_cache_db_path:
            from tradingagents.agent_harness.core.persistent_plan_cache import (
                PersistentPlanCache,
            )
            self.plan_cache: PersistentPlanCache = PersistentPlanCache(
                db_path=plan_cache_db_path,
                ttl_seconds=plan_cache_ttl_seconds,
                max_entries=plan_cache_max_entries,
            )
        else:
            self.plan_cache: PlanTemplateCache = PlanTemplateCache()

    # ------------------------------------------------------------------
    # §P3-2 — per-turn memory helpers
    # ------------------------------------------------------------------
    _SESSION_CTX_KEY = "__session_ctx__"

    @classmethod
    def _session_ctx_key(cls, session_id: str) -> str:
        """L2 namespace: same __session_ctx__ key shape, scoped per
        session so multi-session carry-forward keeps working without
        re-introducing the session_id-as-user_id misuse."""
        return f"{cls._SESSION_CTX_KEY}:{session_id}"

    def _load_session_context(self, session_id: str) -> dict:
        """Read previous-turn metadata from L2 (symbols / intent / user_msg).

        Used at the top of :meth:`stream_chat` to carry forward symbols
        when the current user_message has none (e.g. "加入关注" without
        naming a ticker). Returns empty dict on no memory / no prior turn.
        """
        if self.memory is None or not session_id:
            return {"symbols": [], "intent": None, "user_msg": None}
        try:
            # L2 is user-scoped; namespace the session ctx under
            # ``__session_ctx__:<session_id>`` so multiple sessions of
            # the same user don't trample each other.
            entry = self.memory.l2.get(
                self._session_ctx_key(session_id),
                user_id="default",
            )
        except Exception as e:
            LOGGER.warning("load_session_context failed: %s", e)
            return {"symbols": [], "intent": None, "user_msg": None}
        if entry is None or entry.value is None:
            return {"symbols": [], "intent": None, "user_msg": None}
        if not isinstance(entry.value, dict):
            return {"symbols": [], "intent": None, "user_msg": None}
        return {
            "symbols": list(entry.value.get("symbols") or []),
            "intent": entry.value.get("intent"),
            "user_msg": entry.value.get("user_msg"),
        }

    def _save_turn_summary(
        self,
        session_id: str,
        *,
        user_msg: str,
        intent: Any,
        symbols: list,
        assistant_summary: str | None,
    ) -> None:
        """Persist chat history to L1 + per-turn metadata to L2.

        L1 entries make the next turn's ChatHistoryLayerProvider.collect()
        return non-empty history. L2 metadata enables carry-forward of
        symbols / intent across turns in the same session.
        """
        if self.memory is None or not session_id:
            return
        try:
            self.memory.l1.append_message(session_id, "user", user_msg or "")
            if assistant_summary:
                self.memory.l1.append_message(
                    session_id, "assistant", str(assistant_summary)[:2000],
                )
            from tradingagents.agent_harness.core.tier import sanitize_symbols
            sanitized = sanitize_symbols(symbols)
            self.memory.l2.set(
                self._session_ctx_key(session_id),
                {
                    "symbols": sanitized,
                    "intent": getattr(intent, "value", str(intent) if intent else None),
                    "user_msg": (user_msg or "")[:200],
                },
                user_id="default",
            )
            # §Step 21 — write a cross-session discussion record to L3
            # so the next turn (or a turn in a different session) can
            # surface "earlier we discussed X" context. TTL is 7 days
            # because references older than a week are rarely relevant.
            try:
                self.memory.l3.set(
                    f"discussions:{session_id}",
                    {
                        "session_id": session_id,
                        "symbols": sanitized,
                        "summary": (user_msg or "")[:200],
                        "intent": getattr(intent, "value", str(intent) if intent else None),
                    },
                    session_id=session_id,
                    ttl_seconds=7 * 86400,
                )
            except Exception:
                pass  # L3 write best-effort — never fail the turn
        except Exception as e:
            LOGGER.warning("save_turn_summary failed: %s", e)

    @staticmethod
    def _extract_assistant_summary(final: Any) -> str | None:
        """Best-effort assistant summary extraction from a final payload.

        Mirrors the inline extraction in stream_chat()'s success block
        so the Tier 1 path can reuse it.
        """
        if not isinstance(final, dict):
            return None
        summary = final.get("summary")
        if isinstance(summary, str):
            return summary
        if summary is None and isinstance(final.get("result"), dict):
            return final["result"].get("summary")
        return None

    # ------------------------------------------------------------------
    # Public streaming entry
    # ------------------------------------------------------------------
    async def stream_chat(
        self,
        session_id: str,
        user_message: str,
        *,
        history: list | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Top-level orchestration. Yields SSE-shaped ``(event, payload)``."""
        # §P3-3 — use the entity × op classifier for tier routing too
        # (not just for state.op).  Without this, fast_route() sees a
        # CRUD entity keyword like '关注' / '笔记' / '定时' and forces
        # Tier 1 DIRECT (because legacy classify_intent returns
        # WATCHLIST/NOTE/SCHEDULED for those keywords), which means
        # CRUD writes (add_to_watchlist / create_note / ...) never
        # reach the dispatch table — they hit the short_circuit which
        # only knows list_watchlist / list_scheduled_tasks.
        #
        # fast_route_with_op() routes READ/LIST → Tier 1 (short-circuit
        # works fine for those) and CREATE/UPDATE/DELETE/RUN → Tier 2
        # PLAN_EXECUTE so _plan() can dispatch via _CRUD_DISPATCH.
        from .tier import (
            classify as _classify,
            classify_multi as _classify_multi,
            fast_route_with_op,
        )
        intent, op = _classify(user_message)
        # §Step 20 — load the previous turn's session context before
        # routing so carry-forward symbols can populate the route when
        # the user message has no ticker ("加入我的关注" after
        # discussing 600036). Without this pre-load the routing is
        # symbol-blind and CRUD dispatch sees an empty symbol.
        _session_ctx = self._load_session_context(session_id)
        _carry = list(_session_ctx.get("symbols") or [])
        route, op_from_route = fast_route_with_op(
            user_message, carry_symbols=_carry,
        )
        # §P3-3+: detect multi-intent CRUD queries ("看看告警和笔记")
        # so the plan layer can fan out to multiple tools.
        multi_pairs = _classify_multi(user_message)
        if len(multi_pairs) >= 2:
            primary = (intent, op)
            extra = [pair for pair in multi_pairs if pair != primary]
        else:
            extra = []
        # Reconcile: state.op comes from classify() (more precise for
        # our 8-case vocabulary); the route's op matches but keep both
        # for backward compat with tests that inspect state.op directly.
        assert op == op_from_route or op_from_route in (Op.LIST, Op.READ) or op in (Op.LIST, Op.READ), \
            f"classify vs fast_route_with_op disagree: {op} vs {op_from_route}"
        del op_from_route
        # §7.2 #1: degrade Tier 2/3 → Tier 1 when LLM is unavailable
        # (no factory wired, factory not configured, or circuit open).
        # Keeps symbols so short_circuit can serve, forces intent=QUOTE.
        route, degraded = maybe_degrade_to_tier1(
            route, self.llm_factory, self.circuit_breaker,
        )
        # §7.3 #4 — wire the process default cache into the per-session
        # ToolContext so tool invocations during this turn benefit from
        # cached results.  Default cache is a process-wide singleton
        # (see data.cache.get_default_cache); reusing it across turns
        # matches the spec's TTL-based invalidation model.
        from tradingagents.agent_harness.data.cache import get_default_cache
        context = ToolContext(session_id=session_id, tool_cache=get_default_cache())

        # §7.2 #1: when degraded, emit a single visible "warning" event
        # so the user / observability stack sees the fallback.  Emitted
        # here (before the Tier 1 short-circuit runs) via _emit below.
        degraded_reason: str | None = route.reason if degraded else None

        # Q6 / P2-9: session_start hook — plugins get a chance to set up
        # per-session state (load config, open DB cursors, etc.).
        from tradingagents.agent_harness.core.lifecycle import HookContext
        await self.lifecycle.fire("session_start", HookContext(
            name="session_start", session_id=session_id, extra={"user_message": user_message},
        ))

        # Q4 / P1-4: consume pending steers — prepend them to the user
        # message as a directive prefix so the LLM sees them as
        # authoritative guidance for this turn.
        steers = self.control_bus.consume_steers(session_id)
        if steers:
            steer_block = "\n".join(
                f"[STEERED by {m.author}] {m.message}" for m in steers
            )
            user_message = f"{steer_block}\n\n{user_message}"

        # Q4 / P1-4: consume pending next_idle injects — attach as a
        # hidden system hint via user-role prefix; surface events are
        # emitted in chat_history derivation, not here.
        injects = self.control_bus.consume_injects(session_id, when="next_idle")
        if injects:
            inject_block = "\n".join(
                f"[INJECT from {m.author}] {m.message}" for m in injects
            )
            user_message = f"{inject_block}\n\n{user_message}"

        # §P3-2 — symbol carry-forward: route already absorbed carry
        # symbols via fast_route_with_op(carry_symbols=...). We only
        # need to surface the merge into state / log here. ``_carry``
        # is the raw previous-turn list; ``route.symbols`` already
        # includes them when the user didn't name one in the message.
        session_ctx = _session_ctx
        carry_symbols = _carry
        if carry_symbols:
            LOGGER.info(
                "carry-forward symbols from previous turn: %s (session=%s)",
                carry_symbols, session_id,
            )
        from tradingagents.agent_harness.core.tier import sanitize_symbols
        # P0: drop invalid carry-forward tickers (e.g. bare "SS" leftover from
        # earlier regex extraction). Without this filter the next turn's
        # get_quote("SS") returns no_data and LLM hallucinates an answer.
        sanitized_carry = sanitize_symbols(carry_symbols)
        effective_symbols = list(route.symbols) + [
            s for s in sanitized_carry if s not in route.symbols
        ]

        # §Step1 — pull structured parameter slots (time_range /
        # threshold / cron / limit) out of the user message before
        # state construction. The orchestrator passes ``state.slots``
        # to the planner / tool-arg factory as a cheap pre-fill; LLM
        # synthesis is only invoked for slots that come back as None.
        from tradingagents.agent_harness.core.tier import extract_slots
        slots = extract_slots(user_message)
        state = OrchestratorState(
            session_id=session_id,
            user_message=user_message,
            intent=route.intent,
            symbols=effective_symbols,
            carry_symbols=carry_symbols,
            prior_user_msg=session_ctx.get("user_msg"),
            slots=slots,
            op=op,
            extra_crud_dispatch=extra,
        )

        # Session lifecycle (roadmap A2): auto-create or update metadata.
        if self._session_store is not None:
            try:
                existing = self._session_store.get(session_id)
                if existing is None:
                    self._session_store.upsert(Session(id=session_id, title=user_message[:64]))
                else:
                    self._session_store.touch(session_id)
            except Exception:
                LOGGER.debug("session upsert failed", exc_info=True)

        # Crash recovery bookkeeping (P1-7): buffer emitted events so we
        # can checkpoint + replay after restart. Local list, not on state.
        emitted_events: list[tuple[str, dict[str, Any]]] = []
        current_node = NODE_PLANNING
        token_store_holder: dict[str, TokenUsageStore] = {}

        async def _emit(
            ev: str, payload: dict[str, Any]
        ) -> tuple[str, dict[str, Any]]:
            return await self._tag_and_dispatch(
                ev, payload,
                _emitted_events=emitted_events,
                _current_node=current_node,
                _state=state,
                _token_store=token_store_holder.get("store"),
                _session_id=session_id,
            )

        # Tier 1 short-circuit when route lands on it.
        # §P3-3 — even short-circuited turns must record L1 history so
        # cross-session references / next-turn carry-forward see them.
        # Previously this path returned early without touching
        # ``_save_turn_summary`` (only Tier 2/3 did), which left L1
        # empty for short-circuit turns (e.g. "600036.SS 多少钱" → no
        # history recorded, the next session has no idea we discussed
        # the stock).
        # §Step 18 — symbol-less Tier 1 when the route carries a
        # report_id slot (e.g. "读报告 run-55464f..."). Without this
        # guard every symbol-less read falls through to Tier 2.
        # §Step 42 — also let "看一下这份报告的详情" through Tier 1
        # (short_circuit resolves the latest report_id from history).
        report_id_slot = (state.slots or {}).get("report_id") if state else None
        # §Step 42 — "看一下这份报告的详情" / "看看刚才的报告" /
        # "列出最近一份报告" all want get_report(latest). The user
        # message might classify as LIST or READ (both are read-only
        # opcodes), so accept either. The short_circuit's
        # _wants_latest_report(message) does the actual disambiguation
        # via hint keywords and resolves the report_id from history.
        wants_latest_report = (
            route.intent == Intent.REPORT
            and route.op in (Op.LIST, Op.READ)
            and not report_id_slot
        )
        if route.tier == Tier.DIRECT and (route.symbols or report_id_slot or wants_latest_report):
            if degraded_reason:
                # §7.2 #1: surface the LLM-unavailable fallback so users
                # (and the audit log) know why we skipped Tier 2/3.
                yield await _emit("warning", {
                    "message": degraded_reason,
                    "fallback": "tier1_short_circuit",
                    "tier": int(route.tier),
                })
            # Capture the final payload so _save_turn_summary can write
            # an assistant summary. The short-circuit always emits an
            # ``agent_final`` event with ``result`` containing the
            # tool output; we stash it in state.final.
            # §Step4 — propagate state.slots so list_* read tools honour
            # user-supplied limit / include_disabled in Tier 1 short-circuit.
            async for ev, payload in self._short_circuit.run(route, user_message, context, slots=state.slots):
                if ev == "agent_final":
                    state.final = payload
                yield await _emit(ev, payload)
            self._save_turn_summary(
                session_id,
                user_msg=user_message,
                intent=route.intent,
                symbols=list(route.symbols or []),
                assistant_summary=self._extract_assistant_summary(state.final),
            )
            # Step 29 — checkpoint cleanup. Tier 1 short-circuit
            # returns BEFORE the long-form success path at line 1110,
            # so it needs its own delete to drop every milestone row
            # for this session. Without this, the partial-replay
            # chain (planning:1..N) survives the run and resume()
            # would replay all of them on a subsequent reconnect.
            if self._checkpoint_store is not None:
                try:
                    self._checkpoint_store.delete(session_id)
                except Exception:
                    LOGGER.debug("checkpoint cleanup (tier1) failed", exc_info=True)
            return

        # Token accounting: every LLM call inside stream_chat records
        # into this per-session store (P1 TokenUsage disjoint). Surfaces
        # as ``usage_summary`` SSE event so the frontend can render
        # per-agent cost attribution.
        store = TokenUsageStore()
        token_store_holder["store"] = store
        try:
            with attach_store(store):
                
                try:

                    async for ev in self._stream_chat_impl(state, context, route, _emit):

                        yield ev

                finally:

                    # Q4 / P1-4: fire whenIdle callbacks regardless of

                    # success / error / early-return so scheduled tasks

                    # and observability hooks see every turn complete.

                    try:

                        await self.control_bus.notify_idle(session_id, state=state)

                    except Exception:

                        LOGGER.debug("notify_idle failed", exc_info=True)

                    # Q6 / P2-9: session_end hook (mirrors notify_idle
                    # pattern — runs even on early return / error).
                    try:

                        await self.lifecycle.fire("session_end", HookContext(
                            name="session_end", session_id=session_id,
                            extra={"turn_id": getattr(state.turn, "turn_id", None)},
                        ))

                    except Exception:

                        LOGGER.debug("session_end hook failed", exc_info=True)
            current_node = NODE_DONE
            # §Step 9 — surface PlanTemplateCache stats alongside the
            # usage_summary so the UI / debug layer can see how often
            # the LLM plan step was skipped.
            # §Step 14 — also feed the same counters into the
            # TokenUsageStore so usage_summary.totals.cache_extractions
            # is self-contained (single round-trip for the UI).
            try:
                cache_stats = self.plan_cache.stats()
                if cache_stats:
                    # Feed into the per-session store so the usage_summary
                    # event includes cache_extractions / cache_misses.
                    try:
                        store.record_cache(
                            agent="orchestrator",
                            hits=int(cache_stats.get("hits", 0)),
                            misses=int(cache_stats.get("misses", 0)),
                            evictions=int(cache_stats.get("evictions", 0)),
                            expirations=int(cache_stats.get("expirations", 0)),
                        )
                    except Exception:
                        LOGGER.debug("record_cache failed", exc_info=True)
                    # Keep the standalone event so debug surfaces still see it.
                    yield await _emit("cache_stats", cache_stats)
            except Exception:
                LOGGER.debug("cache_stats emit failed", exc_info=True)
            yield await _emit("usage_summary", store.summary())
            # §P3-2 — persist user message + assistant final so the next
            # turn's ChatHistoryLayerProvider returns real history and
            # the symbol carry-forward (L2 __session_ctx__) sees the
            # assets discussed in this turn.
            try:
                final_payload = state.final
                assistant_text = None
                if isinstance(final_payload, dict):
                    summary = final_payload.get("summary")
                    if isinstance(summary, str):
                        assistant_text = summary
                    elif summary is None and isinstance(final_payload.get("result"), dict):
                        assistant_text = final_payload["result"].get("summary")
                self._save_turn_summary(
                    session_id,
                    user_msg=state.user_message,
                    intent=state.intent,
                    symbols=state.symbols,
                    assistant_summary=assistant_text,
                )
            except Exception:
                LOGGER.debug("save_turn_summary outer failed", exc_info=True)
            # Session accounting (A2): bump token_total on success.
            if self._session_store is not None:
                try:
                    self._session_store.touch(
                        session_id,
                        token_delta=store.totals().get("total_tokens", 0),
                    )
                except Exception:
                    LOGGER.debug("session touch failed", exc_info=True)
            # Success: drop the checkpoint so resume() doesn't replay.
            if self._checkpoint_store is not None:
                try:
                    self._checkpoint_store.delete(session_id)
                except Exception:
                    LOGGER.debug("checkpoint cleanup failed", exc_info=True)
        except Exception as e:
            LOGGER.exception("orchestrator failed")
            state.error = str(e)
            err_payload: dict = {"tier": int(route.tier), "error": str(e)}
            if isinstance(e, LlmFailure):
                err_payload["failure"] = e.to_dict()
            err_payload["usage"] = store.summary()
            yield await _emit("error", err_payload)
            # Q1: close turn with ERROR (defensive — _stream_chat_impl
            # should have done this already; if we got here it's a bug
            # in the inner flow).
            try:
                if state.turn is not None and state.turn.finished_at is None:
                    async for _ev in _close_turn(TurnEndReason.ERROR, error=str(e)):
                        yield _ev
            except Exception:
                LOGGER.debug("outer close_turn failed", exc_info=True)
            return
        return

    async def _tag_and_dispatch(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        _emitted_events: list[tuple[str, dict[str, Any]]] | None = None,
        _current_node: str | None = None,
        _state: OrchestratorState | None = None,
        _token_store: TokenUsageStore | None = None,
        _session_id: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Surface-tag an event before yielding it.

        Side-effects:
        - dispatch to audit/debug sinks via ``SurfaceRouter``
        - append (event, tagged_payload) to ``_emitted_events`` (for replay)
        - call ``_maybe_checkpoint`` if wiring is in place

        The trailing underscore kwargs are filled in by ``stream_chat``
        to thread context through the emit pipeline.  When called
        directly (e.g. from tests) they're None and no checkpointing
        happens.
        """
        self._surface_router.route(event, payload)
        tagged = self._surface_router.tag(event, payload)
        if _emitted_events is not None:
            _emitted_events.append((event, tagged))
        if (
            self._checkpoint_store is not None
            and _state is not None
            and _current_node is not None
            and _session_id is not None
        ):
            self._maybe_checkpoint(
                session_id=_session_id,
                state=_state,
                node_position=_current_node,
                emitted_events=_emitted_events or [],
                token_store=_token_store,
            )
        return event, tagged

    def _maybe_checkpoint(
        self,
        session_id: str,
        state: OrchestratorState,
        node_position: str,
        emitted_events: list[tuple[str, dict[str, Any]]],
        token_store: TokenUsageStore,
    ) -> None:
        """Persist the current session state if a checkpoint store is wired.

        Best-effort: failures are logged but never break the orchestrator.
        Cheap (sub-ms SQLite write). Called after every emit so resume()
        can replay + continue.
        """
        if self._checkpoint_store is None:
            return
        if node_position not in ALL_NODES:
            return  # intermediate position, don't checkpoint yet
        try:
            ckpt = HarnessCheckpoint(
                session_id=session_id,
                node_position=node_position,
                state={
                    "intent": state.intent.value if hasattr(state.intent, "value") else str(state.intent),
                    "symbols": list(state.symbols or []),
                    "user_message": state.user_message,
                    "plan": state.plan,
                    "tool_results": state.tool_results,
                    "final": self._dump(state.final) if state.final is not None else None,
                    "error": state.error,
                },
                emitted_events=list(emitted_events),
                token_usage=token_store.summary() if token_store is not None else {},
            )
            self._checkpoint_store.save(ckpt)
        except Exception:
            LOGGER.debug("checkpoint save failed", exc_info=True)

    async def resume_from(
        self,
        session_id: str,
        from_node: str | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Step 29 — partial replay from a specified node.

        ``from_node`` must be a member of ALL_NODES or None. None
        means "replay everything from the latest checkpoint"
        (equivalent to ``resume()``). A specific node means
        "find the latest milestone whose node is at or before
        ``from_node`` and replay only those events".

        Yields the same event stream as ``resume()`` so the SSE
        client can render them transparently.
        """
        if self._checkpoint_store is None:
            yield ("error", {"session_id": session_id, "error": "checkpoint store not configured"})
            return
        if from_node is None:
            ckpt = self._checkpoint_store.load_latest(session_id)
        else:
            ckpt = self._checkpoint_store.load_at_or_before(session_id, from_node)
        if ckpt is None:
            yield ("error", {
                "session_id": session_id,
                "error": "no checkpoint for session",
                "from_node": from_node,
            })
            return
        for ev, payload in ckpt.emitted_events:
            yield (ev, payload)
        yield ("resume_complete", {
            "session_id": session_id,
            "from_node": from_node,
            "milestone_id": ckpt.milestone_id,
            "node_position": ckpt.node_position,
            "events_replayed": len(ckpt.emitted_events),
        })

    async def resume(self, session_id: str) -> AsyncIterator[tuple[str, dict]]:
        """Resume a crashed/interrupted session from its last checkpoint.

        Replays all events emitted before the crash, then yields a
        ``resume_complete`` event so the client knows to reconnect.

        Edge cases:
        - No checkpoint → ``error`` event with ``no_checkpoint``
        - Checkpoint completed but not deleted → replays events + done
        """
        if self._checkpoint_store is None:
            yield ("error", {"session_id": session_id, "error": "checkpoint store not configured"})
            return
        ckpt = self._checkpoint_store.load(session_id)
        if ckpt is None:
            yield ("error", {"session_id": session_id, "error": "no checkpoint for session"})
            return
        for ev, payload in ckpt.emitted_events:
            yield (ev, payload)
        yield ("resume_complete", {
            "session_id": session_id,
            "node_position": ckpt.node_position,
            "events_replayed": len(ckpt.emitted_events),
        })

    async def _stream_chat_impl(
        self,
        state: OrchestratorState,
        context: ToolContext,
        route: Any,
        _emit: Callable[[str, dict[str, Any]], Any],
    ) -> AsyncIterator[tuple[str, dict]]:
        """Body of stream_chat — runs inside ``attach_store(store)``.

        Refactored from the original monolithic stream_chat so the
        TokenUsageStore lifetime is explicit. All LLM calls inside
        (plan, synthesize, L3 judge, sub-agents via track_agent) end
        up in the active store.  Every yielded event is also tagged
        with its ``surface`` (P0-4) via the ``_emit`` closure defined
        in ``stream_chat`` (above).

        Q1 / P2-4: emits turn/started + per-step step/started/step/ended,
        then a closing turn/ended with TurnEndReason. The TurnRecord is
        appended to self._turn_history (capped at _turn_history_limit).
        """
        from tradingagents.agent_harness.core.turn_boundary import (
            TurnRecord, TurnEndReason, StepRecord,
        )
        from tradingagents.agent_harness.core.lifecycle import HookContext
        turn = TurnRecord(
            turn_id=self.turn_boundary.next_turn_id(),
            session_id=state.session_id,
            user_message=state.user_message,
            started_at=time.time(),
        )
        state.turn = turn
        state.current_step_id = 0
        yield await _emit("turn/started", {
            "turn_id": turn.turn_id, "session_id": turn.session_id,
        })
        await self.lifecycle.fire("turn_start", HookContext(
            name="turn_start", session_id=turn.session_id, turn_id=turn.turn_id,
        ))

        async def _step_start(name):
            state.current_step_id += 1
            state.current_step_started_at = time.time()
            step = StepRecord(
                step_id=state.current_step_id, name=name,
                started_at=state.current_step_started_at,
            )
            turn.steps.append(step)
            await self.lifecycle.fire("step_start", HookContext(
                name="step_start", session_id=turn.session_id,
                turn_id=turn.turn_id, step_id=step.step_id, step_name=step.name,
            ))
            ev = await _emit("step/started", {
                "turn_id": turn.turn_id, "step_id": step.step_id, "name": step.name,
            })
            yield ev

        async def _step_end(name, status, error=None):
            if turn.steps and turn.steps[-1].name == name and turn.steps[-1].finished_at is None:
                last = turn.steps[-1]
                last.finished_at = time.time()
                last.status = status
                last.error = error
            await self.lifecycle.fire("step_end", HookContext(
                name="step_end", session_id=turn.session_id,
                turn_id=turn.turn_id, step_id=state.current_step_id,
                step_name=name, error=error,
                extra={"status": status},
            ))
            ev = await _emit("step/ended", {
                "turn_id": turn.turn_id, "step_id": state.current_step_id,
                "name": name, "status": status,
                "duration_s": (time.time() - state.current_step_started_at),
                "error": error,
            })
            yield ev

        async def _close_turn(reason, error=None):
            turn.finished_at = time.time()
            turn.end_reason = reason
            self._turn_history.append(turn)
            await self.lifecycle.fire("turn_end", HookContext(
                name="turn_end", session_id=turn.session_id, turn_id=turn.turn_id,
                error=error, extra={"end_reason": reason.value},
            ))
            if len(self._turn_history) > self._turn_history_limit:
                self._turn_history = self._turn_history[-self._turn_history_limit:]
            ev = await _emit("turn/ended", {
                "turn_id": turn.turn_id, "session_id": turn.session_id,
                "end_reason": reason.value, "duration_s": turn.duration_s,
                "step_count": turn.step_count, "error": error,
            })
            yield ev

        # Tier 2: 5-node state machine.
        try:
            async for _ev in _step_start("planning"):
                yield _ev
            yield await _emit("plan_started", {"intent": route.intent.value})
            plan = await self._plan(state, context)
            state.plan = plan
            # §7.3 #9: schedule parallel tool calls now that the plan is final.
            # ``_execute`` / ``_execute_ptc`` will await the drain and use the
            # cache for any tool whose result is already in flight.
            self._kick_off_prefetch(state, context)
            # Detect PTC program shape: {"mode": "ptc", "groups": [...]}
            is_ptc = isinstance(plan, dict) and plan.get("mode") == "ptc"
            ev_name = "plan_ready_ptc" if is_ptc else "plan_ready"
            ev_payload = {"groups": plan.get("groups", [])} if is_ptc else {"steps": plan}
            async for _ev in _step_end("planning", "ok"):
                yield _ev
            current_node = NODE_EXECUTING
            yield await _emit(ev_name, ev_payload)

            async for _ev in _step_start("executing"):
                yield _ev
            if is_ptc:
                results = await self._execute_ptc(state, context)
            else:
                results = await self._execute(state, context)
            async for _ev in _step_end("executing", "ok"):
                yield _ev
            state.tool_results = results
            current_node = NODE_OBSERVING
            for r in results:
                yield await _emit("tool_result", r)
            # §P3-3+ HITL: drain pending approvals and emit
            # ``confirm_request`` SSE events so the frontend shows a
            # confirm dialog. One event per gated tool. The frontend
            # POSTs to /api/harness/sessions/{sid}/confirm to grant
            # approval; subsequent stream_chat() runs find
            # is_approved()=True and execute the tool.
            pending = list(getattr(state, "pending_approvals", []) or [])
            for gate in pending:
                yield await _emit("confirm_request", gate)
            state.pending_approvals = []  # consumed

            async for _ev in _step_start("observing"):
                yield _ev
            observations = self._observe(state)
            async for _ev in _step_end("observing", "ok"):
                yield _ev
            yield await _emit("observed", observations)

            async for _ev in _step_start("verifying"):
                yield _ev
            # L1 + L2 first (can short-circuit via plan mutation).
            verification = await self._verify(state)
            async for _ev in _step_end("verifying", "ok"):
                yield _ev
            yield await _emit("verified", {"ok": verification.ok, "level": int(verification.level)})

            async for _ev in _step_start("synthesizing"):
                yield _ev
            final = await self._synthesize(state)
            async for _ev in _step_end("synthesizing", "ok"):
                yield _ev
            state.final = final
            current_node = NODE_DONE
            # §Step 10 — surface scope slot (user / all) alongside the
            # Tier 2/3 agent_final so the UI badge can show
            # '我的笔记' vs '这个资产的笔记'. Tier 1
            # path already includes this in short_circuit.run().
            # §Step 26 — render the final answer into friendly
            # markdown when ``final`` is a dict produced by the
            # trivial-CRUd fast-path or a synthesized payload. The
            # markdown goes in the bubble; the raw ``final`` dict is
            # kept under ``result_raw`` so audit / L3 still see the
            # structured answer.
            final_dump = self._dump(final)
            tool_name_hint = (state.tool_results[0].get("name")
                              if (state.tool_results and isinstance(state.tool_results[0], dict))
                              else None)
            friendly = self._render_synth_friendly(final_dump, tool_name_hint)
            yield await _emit("agent_final", {
                "tier": int(Tier.PLAN_EXECUTE),
                "result": friendly if friendly else final_dump,
                "result_raw": final_dump,
                "scope": (state.slots or {}).get("scope", "user"),
            })

            # L3 LLM-judge (spec §D6) — runs AFTER synthesize. Extracted
            # into `_run_l3_judge` so it's directly testable without
            # spinning up the full 5-node state machine.
            async for ev, payload in self._run_l3_judge(state):
                yield await _emit(ev, payload)

            # §P2 — turn-level synth retry on L3 claim_audit fail.
            # When the L3 verdict is ungrounded AND the failure came
            # specifically from claim_audit (unsupported numbers, not
            # a tool failure), we re-trigger ``_synthesize`` once
            # with a directive naming the unsupported claims. The
            # orchestrator caps total attempts at:
            #   1 initial + intra-synth retry + 1 turn-level retry.
            l3 = getattr(state, "last_l3", None)
            if (
                    l3 is not None
                    and not l3.ok
                    and getattr(state, "synth_retry_count", 0)
                        < self._SYNTH_TURN_RETRY_LIMIT
            ):
                # Only retry when the failure mentions unsupported
                # claims (claim_audit or forward-claim fabrication),
                # NOT for trivial tool-call failures.
                unsupported = (
                    (l3.details or {}).get("claim_audit_unsupported")
                    if isinstance(l3.details, dict) else None
                ) or []
                is_claim_fail = bool(unsupported) or any(
                    "forward" in (r or "").lower()
                    or "claim_audit" in (r or "").lower()
                    or "缺乏出处" in (r or "")
                    for r in (l3.issues or [])
                )
                if is_claim_fail:
                    state.synth_retry_count += 1
                    state.synth_retry_directive = (
                        self._SYNTH_TURN_RETRY_HINT_TMPL.format(
                            unsupported=", ".join(
                                str(u) for u in unsupported[:6]
                            ) or "(see previous issues)"
                        )
                    )
                    LOGGER.info(
                        "synth turn-level retry #%d due to claim_audit fail",
                        state.synth_retry_count,
                    )
                    async for _ev in _step_start("synthesizing"):
                        yield _ev
                    final = await self._synthesize(state)
                    async for _ev in _step_end("synthesizing", "ok"):
                        yield _ev
                    state.final = final
                    # Re-render the friendly summary with the new
                    # ``final`` so the agent_final reflects the
                    # retried answer.
                    final_dump = self._dump(final)
                    friendly = self._render_synth_friendly(final_dump, tool_name_hint)
                    yield await _emit("synth_retried", {
                        "attempt": state.synth_retry_count,
                        "unsupported": unsupported,
                    })
                    yield await _emit("agent_final", {
                        "tier": int(Tier.PLAN_EXECUTE),
                        "result": friendly if friendly else final_dump,
                        "result_raw": final_dump,
                        "scope": (state.slots or {}).get("scope", "user"),
                    })
                    # Re-run L3 to surface the new verdict. The
                    # verdict is appended but we do NOT loop again
                    # (we've already used our turn-level budget).
                    async for ev, payload in self._run_l3_judge(state):
                        yield await _emit(ev, payload)

            if self.audit is not None and hasattr(self.audit, "log"):
                try:
                    self.audit.log(
                        session_id=session_id,
                        event="tier2_complete",
                        payload={"intent": route.intent.value, "plan_steps": len(plan)},
                    )
                except Exception:  # audit must never break the orchestrator
                    LOGGER.debug("audit log failed", exc_info=True)
            # Q1: close turn on successful completion.
            async for _ev in _close_turn(TurnEndReason.USER_DONE):
                yield _ev
        except Exception as e:
            # Q1: close turn on error path; mark the currently-active step
            # as errored so the audit log can pinpoint which node failed.
            try:
                cur = state.turn.steps[-1].name if state.turn and state.turn.steps else "unknown"
                async for _ev in _step_end(cur, "error", error=str(e)):
                    yield _ev
            except Exception:
                LOGGER.debug("step_end on error failed", exc_info=True)
            try:
                if state.turn is not None and state.turn.finished_at is None:
                    async for _ev in _close_turn(TurnEndReason.ERROR, error=str(e)):
                        yield _ev
            except Exception:
                LOGGER.debug("close_turn on error failed", exc_info=True)
            # Errors propagate to outer attach_store block.
            raise

    # ------------------------------------------------------------------
    # 5 nodes
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # §P3-3 — CRUD dispatch table
    # ------------------------------------------------------------------
    # Maps every (entity_intent, op) pair the orchestrator should be able
    # to serve directly to (tool_name, args_factory). The args factory
    # receives the OrchestratorState so it can pull symbols / carry-forward
    # / user message into the tool's required args shape.
    #
    # Adding a new CRUD entity = add ONE line here + register the tool +
    # extend _ENTITY_KW / _OP_KW in tier.py. No plan-layer changes.
    from .tier import Intent as _Intent, Op as _Op
    _CRUD_DISPATCH: dict[tuple[Any, Any], tuple[str, Any]] = {
        # watchlist: no UPDATE — items are only added/removed/reordered
        (_Intent.WATCHLIST, _Op.LIST):   ("list_watchlist",        lambda s: {}),
        (_Intent.WATCHLIST, _Op.READ):   ("list_watchlist",        lambda s: {}),
        (_Intent.WATCHLIST, _Op.CREATE): ("add_to_watchlist",      lambda s: _watchlist_crud_args(s)),
        (_Intent.WATCHLIST, _Op.DELETE): ("remove_from_watchlist", lambda s: _watchlist_crud_args(s)),
        # note: list / create / update / delete
        # §P3-3+ — list/read scoped to the focused symbol (carry-forward
        # or explicit) so the tool returns data already filtered. LLM
        # no longer has to manually pick "this asset's" rows out of
        # global lists.
        (_Intent.NOTE, _Op.LIST):   ("list_notes",   _list_notes_args),
        (_Intent.NOTE, _Op.READ):   ("list_notes",   _list_notes_args),
        (_Intent.NOTE, _Op.CREATE): ("create_note",  lambda s: _note_create_args(s)),
        (_Intent.NOTE, _Op.UPDATE): ("update_note",  lambda s: _note_id_args(s)),
        (_Intent.NOTE, _Op.DELETE): ("delete_note",  lambda s: _note_id_args(s)),
        # alert: list / create / update / delete / bulk-delete
        (_Intent.ALERT, _Op.LIST):   ("list_alerts",     _list_alerts_args),
        (_Intent.ALERT, _Op.READ):   ("list_alerts",     _list_alerts_args),
        (_Intent.ALERT, _Op.CREATE): ("create_alert",    lambda s: _alert_create_args(s)),
        (_Intent.ALERT, _Op.UPDATE): ("update_alert",    lambda s: _alert_id_args(s)),
        (_Intent.ALERT, _Op.DELETE): ("delete_alert",    lambda s: _alert_id_args(s)),
        # §P3-3+ bulk delete: list+soft_delete loop under one HITL gate.
        # Args factory pulls focus symbol from state.symbols (carry-
        # forward or explicit). When no symbol is available, leaves
        # it empty so the tool reports its own validation error rather
        # than silently deleting nothing.
        (_Intent.ALERT, _Op.BULK_DELETE): (
            "delete_alerts_for_symbol",
            lambda s: _alert_bulk_delete_args(s),
        ),
        # §P3-3+ bulk delete for notes (mirror of alerts).
        (_Intent.NOTE, _Op.BULK_DELETE): (
            "delete_notes_for_symbol",
            lambda s: _bulk_by_symbol_args(s),
        ),
        # §P3-3+ bulk delete for scheduled jobs (mirror of alerts).
        (_Intent.SCHEDULED, _Op.BULK_DELETE): (
            "delete_scheduled_tasks_for_symbol",
            lambda s: _bulk_by_symbol_args(s),
        ),
        # scheduled: list / create / update / delete / run-now
        (_Intent.SCHEDULED, _Op.LIST):   ("list_scheduled_tasks",     lambda s: {}),
        (_Intent.SCHEDULED, _Op.READ):   ("list_scheduled_tasks",     lambda s: {}),
        (_Intent.SCHEDULED, _Op.CREATE): ("create_scheduled_task",    lambda s: _scheduled_create_args(s)),
        (_Intent.SCHEDULED, _Op.UPDATE): ("update_scheduled_task",    lambda s: _scheduled_id_args(s)),
        (_Intent.SCHEDULED, _Op.DELETE): ("delete_scheduled_task",    lambda s: _scheduled_id_args(s)),
        (_Intent.SCHEDULED, _Op.RUN):    ("run_scheduled_task",       lambda s: _scheduled_id_args(s)),
        # scheduled: list / create / update / delete / run-now
        # §P3-3+ — list scoped to focused symbol (carry-forward or
        # explicit) so the user gets the jobs for the asset they're
        # currently looking at, not every job in the system.
        (_Intent.SCHEDULED, _Op.LIST):   ("list_scheduled_tasks",     _list_scheduled_tasks_args),
        (_Intent.SCHEDULED, _Op.READ):   ("list_scheduled_tasks",     _list_scheduled_tasks_args),
        # run (analysis): create / list / read / cancel
        # §P3-3+ — list scoped to focused symbol via the same
        # carry-forward path. Uses RunManager.list_runs_for_ticker
        # when symbol is set, falls back to list_runs() otherwise.
        (_Intent.RUN, _Op.LIST):   ("list_runs",              _list_runs_args),
        (_Intent.RUN, _Op.READ):   ("get_analysis_status",    lambda s: _run_id_args(s)),
        (_Intent.RUN, _Op.CREATE): ("run_trading_agents_analysis", lambda s: _run_create_args(s)),
        (_Intent.RUN, _Op.DELETE): ("cancel_analysis_run",    lambda s: _run_id_args(s)),
        # report: list / read
        # §P3-3+ — list scoped to focused symbol (list_reports
        # already takes symbol; just need to wire it).
        (_Intent.REPORT, _Op.LIST): ("list_reports", _list_reports_args),
        (_Intent.REPORT, _Op.READ): ("get_report",   lambda s: _report_id_args(s)),
    }

    @staticmethod
    def _crud_plan_for_state(state: OrchestratorState) -> list[dict[str, Any]]:
        """§P3-3 — single dispatch entry for CRUD (entity, op) pairs.

        Returns a one-step plan ``[{"step": 1, "action": <tool>, "args": ...}]``
        when ``(state.intent, state.op)`` is in :pyattr:`_CRUD_DISPATCH`,
        otherwise an empty list so the caller falls back to the legacy
        data-only heuristic plan. The args factory has access to the
        full OrchestratorState (carry_symbols, user_message, prior_turn)
        so it can build the right arg shape per tool.

        staticmethod so tests can call it without instantiating the
        full Orchestrator (which has heavy collaborators). The dispatch
        table is class-level so the function doesn't need ``self``.

        §P3-3+ — promote single-record DELETE to BULK_DELETE when the
        user said 'this asset's X' with no specific ID and a focused
        symbol is available. Pre-fix the user had to say '都删' /
        '全部删除' for bulk to fire; now '删除这个资产的笔记' also
        routes correctly without explicit bulk markers.
        """
        if state.op is None or state.intent is None:
            return []
        # Promote DELETE -> BULK_DELETE when no ID + focused symbol
        op = state.op
        if _should_promote_to_bulk_delete(state):
            LOGGER.info(
                "promote DELETE -> BULK_DELETE for %s (focused=%s, msg=%r)",
                state.intent, _focused_symbol(state), state.user_message,
            )
            op = Op.BULK_DELETE
        key = (state.intent, op)
        spec = Orchestrator._CRUD_DISPATCH.get(key)
        if spec is None:
            return []
        action, args_fn = spec
        try:
            args = args_fn(state) or {}
        except Exception as e:
            LOGGER.warning("CRUD args factory failed for %s: %s", key, e)
            return []
        return [{"step": 1, "action": action, "args": dict(args)}]

    @staticmethod
    def _multi_crud_plan(state: OrchestratorState) -> dict[str, Any] | None:
        """§P3-3+ — fan a multi-intent state into a PTC parallel plan.

        Walks every (intent, op) pair in ``state.extra_crud_dispatch`` +
        the primary (state.intent, state.op), looks each one up in the
        CRUD dispatch table, and emits one ``get_*`` / ``list_*`` /
        ``create_*`` call per pair. Tools that take the symbol argument
        inherit it from ``state.symbols`` (carry-forward or explicit).

        Returns ``None`` when at least one pair does not resolve to a
        tool (so the caller falls back to single-CRUD or heuristic).
        When all pairs resolve, returns a PTC plan with a single group
        so the executor fires them concurrently.
        """
        pairs: list[tuple[Any, Any]] = list(
            getattr(state, "extra_crud_dispatch", []) or []
        )
        primary = (state.intent, state.op) if state.op is not None else None
        if primary and primary not in pairs:
            pairs = [primary] + pairs
        if not pairs:
            return None

        # §P3-3+ — promote single-record DELETE to BULK_DELETE in the
        # multi-CRUD path too. The helper checks the user message and
        # the focused symbol so '把这个资产的笔记删了' (multi-intent
        # with NOTE+DELETE) lands on delete_notes_for_symbol.
        effective_op = state.op
        if _should_promote_to_bulk_delete(state):
            LOGGER.info(
                "multi-CRUD: promote DELETE -> BULK_DELETE for %s (focused=%s)",
                state.intent, _focused_symbol(state),
            )
            effective_op = Op.BULK_DELETE
            # rewrite the primary pair in the pairs list
            pairs = [(i, o if i != state.intent or o != state.op else effective_op)
                     for (i, o) in pairs]
            # also rewrite the primary if it's in the list
            pairs = [
                (i, effective_op) if (i == state.intent and o == state.op)
                else (i, o)
                for (i, o) in pairs
            ]

        calls: list[dict[str, Any]] = []
        for intent, op in pairs:
            spec = Orchestrator._CRUD_DISPATCH.get((intent, op))
            if spec is None:
                return None  # fall back to single dispatch / heuristic
            action, args_fn = spec
            try:
                args = args_fn(state) or {}
            except Exception as e:
                LOGGER.warning(
                    "multi-CRUD args factory failed for %s: %s", (intent, op), e,
                )
                args = {}
            calls.append({"name": action, "args": dict(args)})

        if not calls:
            return None
        # Wrap into a single PTC group so the executor fires them all
        # concurrently and the synthesizer aggregates the results.
        return {
            "mode": "ptc",
            "groups": [{
                "id": "g1",
                "calls": calls,
            }],
        }

    async def _plan(self, state: OrchestratorState, context: ToolContext) -> list[dict[str, Any]]:
        """PlanNode — turn ``state.user_message`` into a JSON plan.

        The plan is a list of ``{"step": int, "action": tool_name, "args": {...}}``.
        LLM-backed when ``llm_factory`` is wired; falls back to a heuristic
        plan when no LLM is available so the harness remains testable.

        §7.3 #6 (N65 fix): before any LLM call, consult
        ``PlanTemplateCache`` — an identical normalised user_message
        within the last 5 minutes short-circuits the LLM and returns the
        cached plan.  The cache is also populated by every successful
        generation (LLM or heuristic) so the next hit is free.

        §P3-3: CRUD entities (WATCHLIST / NOTE / ALERT / SCHEDULED /
        RUN / REPORT) with a recognised ``state.op`` are dispatched via
        ``self._CRUD_DISPATCH`` (one place; no per-entity branches).
        """
        cached = self.plan_cache.get(state.user_message)
        if cached is not None:
            return cached
        # §P3-3+ multi-intent FIRST (e.g. "看一下笔记和告警").
        # Single-CRUD below short-circuits on the primary pair and
        # would silently drop the secondary tools — multi must win
        # whenever extra_crud_dispatch is non-empty.
        if getattr(state, "extra_crud_dispatch", None):
            multi_plan = self._multi_crud_plan(state)
            if multi_plan is not None:
                self.plan_cache.put(state.user_message, multi_plan)
                return multi_plan
        # §7.3 #12 — single-CRUD dispatch BEFORE LLM plan. The CRUD
        # dispatch table maps (intent, op) -> write/read tool
        # deterministically, so for write intents (create_note /
        # create_alert / add_to_watchlist / ...) the LLM doesn't need
        # to be consulted at all — it would only hallucinate "已添加"
        # without dispatching the tool. Single-CRUD check first so the
        # LLM never sees a CREATE / UPDATE / DELETE / BULK_DELETE op.
        if state.intent is not None and state.op is not None:
            crud_plan = self._crud_plan_for_state(state)
            if crud_plan:
                self.plan_cache.put(state.user_message, crud_plan)
                return crud_plan
        # CRUD 未命中才让 LLM plan —— 只剩读类查询需要 LLM 决定 fan-out。
        if self.llm_factory is not None:
            try:
                plan = await self._llm_plan(state)
                if plan:
                    self.plan_cache.put(state.user_message, plan)
                    return plan
            except Exception as e:
                LOGGER.warning("LLM plan failed, falling back to heuristic: %s", e)

        # Heuristic plan: when 2+ symbols, return a PTC program so the
        # quote calls run concurrently (one group, no dependencies).
        # Single-symbol path keeps the legacy sequential shape.
        if len(state.symbols) >= 2:
            ptc_plan = {
                "mode": "ptc",
                "groups": [{
                    "id": "g1",
                    "calls": [
                        {"name": "get_quote", "args": {"symbol": sym}}
                        for sym in state.symbols
                    ],
                }],
            }
            # Cache the PTC program so a repeat query reuses it.
            self.plan_cache.put(state.user_message, ptc_plan)
            return ptc_plan

        # Single-symbol heuristic: 1 quote step (+ optional fundamentals).
        plan: list[dict[str, Any]] = []
        for idx, sym in enumerate(state.symbols, start=1):
            plan.append({"step": idx, "action": "get_quote", "args": {"symbol": sym}})
        if state.intent == Intent.COMPARE and state.symbols:
            plan.append({
                "step": len(plan) + 1,
                "action": "get_fundamentals",
                "args": {"symbol": state.symbols[0]},
            })
        # CRUD 已在最前面派发;LLM 也未生成 — 退回 heuristic 兜底
        self.plan_cache.put(state.user_message, plan)
        return plan

    def _kick_off_prefetch(
        self, state: OrchestratorState, context: ToolContext,
    ) -> None:
        """§7.3 #9 — schedule parallel tool invocations after plan is known.

        Called by ``stream_chat`` after ``_plan`` populates ``state.plan``.
        Failures / missing tools / unsupported plan shapes are silently
        skipped — pre-fetch is a latency optimisation, never a correctness
        gate.
        """
        if not state.plan:
            return
        try:
            from tradingagents.agent_harness.core.prefetch import Prefetcher
            pf = Prefetcher()
            n = pf.kick_off(
                plan=state.plan,
                context=context,
                tool_registry=self.tool_registry,
            )
            state.prefetcher = pf
            if n:
                LOGGER.debug("prefetch kicked off: %d calls", n)
        except Exception:
            LOGGER.debug("prefetch kick_off failed", exc_info=True)

    @staticmethod
    def _clean_tool_args(tool_name, args, user_message):
        """Defensive arg cleanup before tool invocation.

        Two common LLM planning failures:

        1. **Symbol extraction**: planner pulls the wrong substring
           (``"SS"`` from ``"600036.SS"``, or just ``"600036"``)
           instead of the full ticker. Try to recover from the
           user's original message via a regex sweep.

        2. **Body / free-text extraction**: planner returns the entire
           user message (including the imperative
           ``"帮我给 600036.SS 加一个笔记：哈哈八成"``) as the body.
           Strip common Chinese imperative prefixes so the stored
           note is just the user-authored content.
        """
        import re as _re

        def _extract_ticker(text):
            # NB: Python's ``\b`` and ``\w`` treat Chinese characters
            # as word characters, so ``\b`` won't match between a
            # Chinese char and an ASCII digit. Use explicit ASCII-only
            # lookarounds ``(?<![A-Za-z0-9_.])`` instead.
            if not text:
                return None
            # A-share with exchange suffix: 6 digits + .SS/.SZ/.SH
            m = _re.search(r"(?<![A-Za-z0-9_.])(\d{6}\.(?:SS|SZ|SH))(?![A-Za-z0-9_])", text, _re.IGNORECASE)
            if m:
                return m.group(1).upper()
            # HK 5 digits + .HK / .HKEX
            m = _re.search(r"(?<![A-Za-z0-9_.])(\d{5}\.(?:HK|HKEX))(?![A-Za-z0-9_])", text, _re.IGNORECASE)
            if m:
                return m.group(1).upper()
            # Crypto pair (BTC-USD etc.)
            m = _re.search(r"(?<![A-Za-z0-9_])(BTC|ETH|SOL)[-/](USD|USDT)(?![A-Za-z0-9_])", text, _re.IGNORECASE)
            if m:
                return f"{m.group(1).upper()}-{m.group(2).upper()}"
            # Bare 6-digit code → assume Shanghai
            m = _re.search(r"(?<![A-Za-z0-9_.])(\d{6})(?![A-Za-z0-9_.])", text)
            if m:
                return f"{m.group(1)}.SS"
            # US ticker 1-5 uppercase letters (whole word)
            m = _re.search(r"(?<![A-Za-z])([A-Z]{1,5})(?![A-Za-z0-9_])", text)
            if m and m.group(1).upper() not in _BARE_EXCHANGE_SUFFIXES:
                return m.group(1)
            return None

        # Bare exchange suffixes that should NOT count as a ticker on
        # their own (the planner often grabs these from inside the
        # user's symbol string — e.g. extracting ``"SS"`` from
        # ``"600036.SS"``). If the symbol is exactly one of these,
        # we still consider it malformed and try to recover.
        _BARE_EXCHANGE_SUFFIXES = {"SS", "SZ", "SH", "HK", "HKEX"}

        def _looks_like_ticker(s):
            if not isinstance(s, str) or len(s) < 2:
                return False
            # exact qualified ticker (e.g. 600036.SS)
            if _re.search(r"\.[A-Z]{2,4}$", s):
                return True
            # bare 6-digit A-share code (e.g. 600036)
            if _re.fullmatch(r"\d{6}", s):
                return True
            # US 1-5 letter ticker — but only when it's not a bare
            # exchange suffix (SS/SZ/SH/HK/etc.)
            if _re.fullmatch(r"[A-Z]{1,5}", s):
                return s.upper() not in _BARE_EXCHANGE_SUFFIXES
            return False

        def _clean_note_body(body):
            if not body:
                return body
            patterns = [
                r"^[\s\S]*?(?:笔记|备忘)\s*[:：]\s*",
                r"^[\s\S]*?(?:内容|正文)\s*[:：]\s*",
                r"^帮我(?:给|帮你)\s*[\d\w\.\-]+\s*(?:这个|那个)?\s*(?:资产|股票|标的|代码)?\s*(?:加|添加|新建)?(?:一下|个)?\s*(?:笔记|备忘|标注)\s*[:：]?\s*",
                r"^记(?:录|一下)\s*[:：]?\s*",
                r"^备注\s*[:：]?\s*",
            ]
            cleaned = body
            for pat in patterns:
                new = _re.sub(pat, "", cleaned, count=1)
                if new != cleaned:
                    cleaned = new.strip()
                    break
            return cleaned

        def _mutate(obj, **fields):
            if obj is None:
                return obj
            for k, v in fields.items():
                if v is not None and hasattr(obj, k):
                    setattr(obj, k, v)
            return obj

        if tool_name == "create_note":
            sym = getattr(args, "symbol", None) if not isinstance(args, dict) else args.get("symbol")
            body = getattr(args, "body_md", None) if not isinstance(args, dict) else args.get("body_md")
            if not _looks_like_ticker(sym or ""):
                recovered = _extract_ticker(user_message or "")
                if recovered:
                    args = _mutate(args, symbol=recovered)
            if isinstance(body, str) and user_message and len(body) > len(user_message) * 0.6:
                cleaned = _clean_note_body(body)
                if cleaned and cleaned != body:
                    args = _mutate(args, body_md=cleaned)
        elif tool_name in ("create_alert", "update_alert", "add_to_watchlist"):
            sym = getattr(args, "symbol", None) if not isinstance(args, dict) else args.get("symbol")
            if not _looks_like_ticker(sym or ""):
                recovered = _extract_ticker(user_message or "")
                if recovered:
                    args = _mutate(args, symbol=recovered)

        return args

    async def _execute(
        self,
        state: OrchestratorState,
        context: ToolContext,
    ) -> list[dict[str, Any]]:
        """ExecuteNode — run plan steps, parallelisable (asyncio.gather)."""
        if not state.plan:
            return []

        async def _run_step(step: dict[str, Any]) -> dict[str, Any]:
            name, agent_name = self._resolve_action(step)
            args = step.get("args", {})

            # §7.3 #10 dedupe: 同 session 60s 内同一 (tool, args) 成功过 → 直接
            # 返回 cached result,跳过 approval / execute / pending_approval
            # 弹窗。避免 LLM 反复 plan 同一 destructive tool 导致的"UI 看着
            # 像撤销、其实早就成功"的混乱。
            cached = _dedupe_lookup(context.session_id, name, args)
            if cached is not None:
                return {"name": name, "result": cached, "deduped": True}

            # Agents without tools (planner / verifier / synthesizer) are
            # handled by separate orchestrator nodes, not by ExecuteNode.
            # Treat their steps as no-op markers so they don't error.
            if agent_name and name == "":
                return {
                    "name": agent_name,
                    "result": {"status": "delegated", "note": f"{agent_name} handled outside ExecuteNode"},
                }

            try:
                tool = self.tool_registry.get(name)
            except KeyError:
                return {"name": name, "error": f"unknown tool {name!r}"}

            # Coerce dict plan args → args_schema instance.
            try:
                schema_cls = tool.schema.args_schema
                if isinstance(args, dict) and hasattr(schema_cls, "model_validate"):
                    args = schema_cls.model_validate(args)
            except Exception as e:
                return {"name": name, "error": f"args coerce failed: {e}"}

            # §P3-3+ — defensive arg cleanup. The planner LLM often
            # extracts symbols incorrectly (e.g. ``"SS"`` from
            # ``"600036.SS"``) or includes the user's full message in
            # free-form text fields like ``body_md``. Run a small
            # cleanup pass before the tool sees the args so the user
            # sees sensible data in the approval modal.
            args = self._clean_tool_args(name, args, getattr(state, "user_message", None))

            if (
                not self.llm_factory
                and name in {"create_alert", "update_alert", "delete_alert",
                             "delete_alerts_for_symbol"}
            ):
                # Write tools require LLM-backed approval in production; in
                # tests we surface the HITL payload directly. Stash the
                # gate payload on state so the post-execute hook emits a
                # ``confirm_request`` SSE event.
                gate_payload = {
                    "tool_name": name,
                    "args": self._dump(args),
                    "impact": {"reason": "destructive_tool_requires_approval"},
                    "session_id": context.session_id,
                }
                try:
                    state.pending_approvals.append(gate_payload)
                except AttributeError:
                    state.pending_approvals = [gate_payload]
                return {
                    "name": name,
                    "result": {"status": "pending_approval", "args": self._dump(args), "tool_name": name},
                }

            # Funnel through the 5-stage pipeline (pre/guard/exec/post/result).
            # Pipeline handles HITL approval (delete_*/cancel_*), retry policy
            # wrapping, and result observation. Falls back to direct invoke if
            # circuit breaker is open.
            async def _executor(a, c):
                if not self.circuit_breaker.allow():
                    raise RuntimeError("circuit breaker open")
                # retry_async wraps the actual tool.invoke for ProviderError skip
                async def _call() -> Any:
                    return await retry_async(
                        lambda: tool.invoke(a, c),
                        policy=self.retry_policy,
                        skip_exceptions=(ProviderError,),
                    )
                return await _call()

            # Q6 / P2-9: pre_tool_use hook — plugins can intercept /
            # modify args or deny the call entirely. ``any_deny`` short
            # circuits this step (no further tool invocation, no
            # circuit_breaker side effect).
            from tradingagents.agent_harness.core.lifecycle import (
                HookContext, any_deny,
            )
            pre_results = await self.lifecycle.fire("pre_tool_use", HookContext(
                name="pre_tool_use",
                session_id=context.session_id,
                turn_id=getattr(state.turn, "turn_id", None),
                tool_name=name, args=args,
            ))
            if any_deny(pre_results):
                return {"name": name, "result": {"status": "denied_by_hook"}}

            pipe_result = await self._tool_pipeline.run(
                tool_name=name, args=args, tool_context=context,
                executor=_executor,
            )
            # Q6 / P2-9: post_tool_use hook — fires regardless of
            # success / failure so audit / telemetry can observe every
            # tool call without monkey-patching the pipeline.
            post_payload = HookContext(
                name="post_tool_use",
                session_id=context.session_id,
                turn_id=getattr(state.turn, "turn_id", None),
                tool_name=name, args=args,
                result=pipe_result.result if pipe_result.ok else None,
                error=pipe_result.error if not pipe_result.ok else None,
            )
            await self.lifecycle.fire("post_tool_use", post_payload)

            if pipe_result.ok:
                self.circuit_breaker.record_success()
                dumped = self._dump(pipe_result.result)
                # dedupe: 缓存成功的 result,后续重复调用直接命中
                _dedupe_record(context.session_id, name, args, dumped)
                # §P3-3+ HITL via tool-internal gate: when a write tool
                # itself returns ``{"status": "pending_approval", "gate": {...}}``
                # (the bridge path used by create_note / update_note /
                # create_alert etc.), the pipeline sees a successful
                # invocation (pipe_result.ok=True) so the needs_approval
                # branch below never fires and the ``confirm_request`` SSE
                # event is never emitted. Detect this case explicitly and
                # stash the gate_payload so the post-execute drain in
                # ``stream_chat`` emits ``confirm_request`` for the UI banner.
                if isinstance(dumped, dict) and dumped.get("status") == "pending_approval":
                    gate_payload = {
                        "tool_name": name,
                        "args": self._dump(args),
                        "impact": (dumped.get("gate") or {}).get("impact")
                            or {"reason": "destructive_tool_requires_approval"},
                        "session_id": context.session_id,
                    }
                    if "gate" in dumped:
                        gate_payload["gate"] = dumped["gate"]
                    try:
                        state.pending_approvals.append(gate_payload)
                    except AttributeError:
                        state.pending_approvals = [gate_payload]
                    return {"name": name, "result": dumped}
                return {"name": name, "result": dumped}
            if pipe_result.needs_approval:
                # HITL: dangerous tool requires approval. Stash a
                # gate_payload on state so the post-execute drain
                # emits a ``confirm_request`` SSE event for the
                # frontend. Existing pending_approval result shape
                # preserved for backward compat with callers that
                # pattern-match on it.
                gate_payload = {
                    "tool_name": name,
                    "args": self._dump(args),
                    "impact": getattr(
                        pipe_result, "ask_payload", None,
                    ) or {"reason": "destructive_tool_requires_approval"},
                    "session_id": context.session_id,
                }
                try:
                    state.pending_approvals.append(gate_payload)
                except AttributeError:
                    state.pending_approvals = [gate_payload]
                return {
                    "name": name,
                    "result": {
                        "status": "pending_approval",
                        "args": self._dump(args),
                        "tool_name": name,
                    },
                }
            # denied or error
            self.circuit_breaker.record_failure()
            return {"name": name, "error": pipe_result.error}

        return list(await asyncio.gather(*(_run_step(s) for s in state.plan)))

    # ------------------------------------------------------------------
    # PTC branch — concurrent execution of tool-call groups
    # ------------------------------------------------------------------
    async def _execute_ptc(
        self, state: OrchestratorState, context: ToolContext,
    ) -> list[dict[str, Any]]:
        """Execute a PTC program (``state.plan`` is ``{mode:ptc, groups:[...]}``).

        Returns a flat list of results shaped like ``_execute``::

            [{"name": str, "ok": bool, "result": Any|None,
              "error": str|None, "group_id": str}, ...]

        PTC failures don't short-circuit the orch (each failure is a per-tool
        result entry; downstream Observe/Verify nodes consume them).
        """
        # §7.3 #9: drain prefetcher before PTC dispatch.
        if state.prefetcher is not None:
            try:
                await state.prefetcher.drain(timeout=5.0)
            except Exception:
                LOGGER.debug("prefetch drain failed", exc_info=True)
        if not isinstance(state.plan, dict):
            # Defensive — caller should have routed via stream_chat dispatch
            return [{"name": "ptc", "error": "state.plan is not a PTC program"}]
        # LLM-emitted PTC may use "agent" instead of "name"; normalise
        # so parse_program sees the executor-facing field.
        plan_norm = self._normalise_ptc_plan(state.plan)
        try:
            program = parse_program(plan_norm)
        except Exception as e:
            return [{"name": "ptc", "error": f"ptc parse failed: {e}"}]

        if program.is_empty():
            return []

        raw = await self._ptc_executor.execute(program, context)
        # Track circuit-breaker on per-call basis (best-effort)
        for r in raw:
            if r.get("ok"):
                self.circuit_breaker.record_success()
            else:
                self.circuit_breaker.record_failure()
        return raw

    @staticmethod
    def _normalise_ptc_plan(plan: dict[str, Any]) -> dict[str, Any]:
        """Translate LLM-shaped PTC (``agent`` field) into executor-shaped
        PTC (``name`` field).  Mirrors the agent→first-tool mapping used in
        ``_resolve_action`` for sequential plans.

        Defensive: returns the input untouched if it doesn't pass
        ``_validate_ptc`` (e.g. ``groups`` is a string).  Caller is
        expected to either validate beforehand or catch the resulting
        ``parse_program`` error downstream.
        """
        if not Orchestrator._validate_ptc(plan):
            return plan  # leave for downstream error path
        # agent → first tool name (must mirror _resolve_action)
        agent_to_first_tool = {
            "data_agent": "get_quote",
            "alpha_agent": "list_alpha_factors",
            "news_agent": "get_news",
        }
        groups_out = []
        for g in plan.get("groups", []):
            calls_out = []
            for c in g.get("calls", []):
                c2 = dict(c)
                if "name" not in c2 and c2.get("agent"):
                    c2["name"] = agent_to_first_tool.get(c2["agent"], c2["agent"])
                calls_out.append(c2)
            g2 = dict(g)
            g2["calls"] = calls_out
            groups_out.append(g2)
        return {"mode": plan.get("mode", "ptc"), "groups": groups_out}

    def _observe(self, state: OrchestratorState) -> dict[str, Any]:
        """ObserveNode — collapse tool_results into a compact summary."""
        errors = [r for r in state.tool_results if isinstance(r, dict) and "error" in r]
        return {
            "intent": state.intent.value,
            "tool_count": len(state.tool_results),
            "errors": errors,
        }

    async def _verify(self, state: OrchestratorState) -> Any:
        """VerifyNode — run L1 + L2 (+ L3 if enabled) checks."""
        l1_ok = True
        for r in state.tool_results:
            check = self._verifier.verify_l1({"name": r.get("name", "")}, r.get("result"))
            if not check.ok:
                l1_ok = False
                break

        if not l1_ok:
            # Plan-first retry: bump plan with re-attempt entry.
            state.plan.append({
                "step": len(state.plan) + 1,
                "action": "get_quote",
                "args": {"symbol": state.symbols[0]} if state.symbols else {},
            })
        l2 = self._verifier.verify_l2(state.intent.value, state.tool_results)

        # L3 LLM-judge intentionally NOT run here — runs in
        # `_run_l3_judge` AFTER `_synthesize` (otherwise llm_answer is empty
        # and L3 scores 0 with a misleading "verified: fail" event).
        return l2

    async def _run_l3_judge(
        self, state: OrchestratorState
    ) -> AsyncIterator[tuple[str, dict]]:
        """L3 LLM-judge — runs AFTER synthesize so the judge has the answer.

        On ungrounded verdict, appends a replan entry to ``state.plan``
        (spec §D6: "back-to-plan on fail"). Yields ``answer_verified``
        event regardless of verdict; replan is a side-effect on state.plan.
        """
        if not (
            self.enable_l3
            and self._verifier.should_run_l3(state.tool_results, enable_l3=True)
        ):
            return
        try:
            llm_answer = (
                state.final.get("summary", "")
                if isinstance(state.final, dict)
                else _stringify(state.final)
            )
            l3 = await self._verifier.verify_l3(
                user_query=state.user_message,
                tool_results=state.tool_results,
                llm_answer=llm_answer,
            )
            # §P2 — stash on state so ``_stream_chat_impl`` can decide
            # whether to trigger a turn-level synth retry.
            state.last_l3 = l3
            if not l3.ok:
                # Spec §D6 back-to-plan on fail.
                state.plan.append({
                    "step": len(state.plan) + 1,
                    "action": "synthesize",
                    "args": {"replan_reason": l3.reason, "judge": l3.details or {}},
                })
            yield (
                "answer_verified",
                {
                    "ok": l3.ok,
                    "level": int(l3.level),
                    "reason": l3.reason,
                    "details": l3.details,
                },
            )
        except Exception as e:
            LOGGER.warning("L3 verification (post-synth) failed: %s", e)

    async def _synthesize(self, state: OrchestratorState) -> Any:
        """SynthesizeNode — turn tool results into a final answer.

        Fast-path: if every tool result is a trivial CRUD ack
        (``status in {ok, created, updated, deleted, duplicate, pending_approval}``
        with no real data worth analysing), skip the LLM round-trip and
        return a templated summary. This is the common case for short
        imperative messages ("删掉这条笔记", "建一个告警") where the LLM
        would just regurgitate the ack string.
        """
        fast = self._trivial_crud_summary(state.tool_results)
        if fast is not None:
            return fast
        if self.llm_factory is not None:
            try:
                return await self._llm_synthesize(state)
            except Exception as e:
                LOGGER.warning("LLM synthesize failed, returning raw tool_results: %s", e)
        # §14.3.1 — even when LLM synthesize fails, fill ``summary`` from
        # result_formatter so the assistant bubble doesn't dump raw JSON.
        # Trivial-ack results get templated summaries; data-bearing
        # results fall back to a generic "数据已获取,但 LLM 暂时不可用"
        # message so the user knows what happened.
        from .result_formatter import summarize_tool_results
        summary = summarize_tool_results(state.tool_results)
        if not summary:
            sym_part = (
                f" ({', '.join(state.symbols)})" if state.symbols else ""
            )
            summary = f"数据已获取{sym_part},但 LLM 暂时不可用,请重试或换一种问法。"
        return {
            "intent": state.intent.value,
            "symbols": state.symbols,
            "results": state.tool_results,
            "summary": summary,
        }

    def _render_synth_friendly(
        self, final_dump: Any, tool_name_hint: str | None
    ) -> str | None:
        """Tier 2 ``agent_final`` friendly-summary chooser.

        Background: ``final_dump`` from the synth_node is a *wrapper*
        dict (``{intent, symbols, results, summary}``), not the raw
        tool payload. Feeding it to ``_friendly_summary`` makes the
        per-tool renderers (``_render_quote`` etc.) look for top-level
        fields they don't find and fall back to ``"?"``/``None``,
        producing broken bubbles like ``"📈 ? · ¥None"`` even when the
        underlying tool result is fine and the LLM wrote a perfect
        ``summary``.

        Resolution: if the payload looks like a synth wrapper
        (``summary`` or ``results`` key present), prefer ``summary``
        and skip the per-tool renderer. Otherwise delegate to
        ``_friendly_summary`` (covers single-tool payloads promoted
        into Tier 2).
        """
        if not isinstance(final_dump, dict):
            return None
        synth_summary = final_dump.get("summary")
        is_synth_payload = (
            isinstance(synth_summary, str)
            or "results" in final_dump
            or "intent" in final_dump
        )
        if is_synth_payload:
            return synth_summary if isinstance(synth_summary, str) else None
        return self._friendly_summary(
            final_dump, tool_name=tool_name_hint,
        )

    def _friendly_summary(
        result: Any,
        *,
        tool_name: str | None = None,
        intent: str | None = None,
    ) -> str:
        """§Step 26 — render a tool result into a friendly markdown
        summary for the agent_final bubble.

        Routing order:

        1. ``intent`` (when explicit, e.g. multi-intent Tier 1).
        2. ``tool_name`` → look up ``metadata["display_view"]`` on the
           tool's schema. Falls back to intent inference from the
           tool_name (``get_quote`` → quote, etc.).
        3. Graceful JSON dump when nothing matches.

        This is the same family of renderers as
        ``tools/display_view.py:display_view_for`` — we duplicate the
        logic here so the orchestrator doesn't need to reach into the
        tools package (which would create a circular import).
        """
        if not isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False, default=str)

        from tradingagents.agent_harness.tools.display_view import (
            display_view_for,
        )

        view_key = None
        if intent:
            view_key = intent
        if not view_key and tool_name:
            try:
                # Try the registry first — if the tool is registered
                # with metadata we read display_view from there.
                from tradingagents.agent_harness.tools.registry import (
                    get_default_tool_registry,
                )
                tool = get_default_tool_registry().get(tool_name)
                view_key = tool.schema.metadata.get("display_view")
            except Exception:
                pass
        # Last-resort inference from the tool name prefix.
        if not view_key and tool_name:
            if tool_name.startswith("get_quote"):
                view_key = "quote"
            elif tool_name.startswith("get_history"):
                view_key = "history"
            elif tool_name.startswith("get_fundamentals"):
                view_key = "fundamentals"
            elif tool_name.startswith("get_news"):
                view_key = "news"
            elif tool_name.startswith(("list_",)):
                view_key = "list"
            elif tool_name.startswith(("create_", "update_", "delete_",
                                        "add_", "remove_", "run_",
                                        "cancel_")):
                view_key = "ack"
            elif tool_name == "get_report":
                view_key = "report_read"

        if view_key:
            return display_view_for(result, intent=view_key)
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _trivial_crud_summary(tool_results: list) -> dict | None:
        """Return a templated summary if every result is a CRUD ack.

        Returns ``None`` if any result carries meaningful data (quotes,
        news items, fundamentals, factor values, etc.) and therefore
        needs an LLM to phrase the answer.

        §3.4 — delegates per-item templating to
        :func:`result_formatter.summarize_tool_results` (single source
        of truth) and falls back to :func:`_format_trivial_summary` for
        batch-level semantics (e.g. "等待你确认" suffix on
        pending_approval).
        """
        if not tool_results:
            return None
        # Statuses we treat as trivial. Anything else (quote, news,
        # fundamentals, factor list) needs the LLM.
        trivial_statuses = {"ok", "created", "updated", "deleted", "duplicate", "pending_approval", "empty"}
        for r in tool_results:
            if not isinstance(r, dict):
                return None
            status = r.get("status")
            if status not in trivial_statuses:
                return None
            # ``ok`` may carry real data (e.g. a quote snapshot). Detect
            # by presence of typical data-bearing fields.
            if status == "ok":
                if any(r.get(k) for k in ("price", "items", "factors", "alerts", "notes", "rows")):
                    return None
        # Use result_formatter (single source of truth) + add
        # "等待你确认" suffix when any result is pending_approval.
        from .result_formatter import summarize_tool_results
        summary = summarize_tool_results(tool_results)
        if any(r.get("status") == "pending_approval" for r in tool_results if isinstance(r, dict)):
            if summary:
                summary = summary + "\n等待你确认"
            else:
                summary = "等待你确认"
        return {
            "intent": "crud",
            "symbols": [],
            "results": tool_results,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # LLM hooks (only when llm_factory is wired)
    # ------------------------------------------------------------------
    async def _llm_plan(self, state: OrchestratorState):
        """Real LLM-backed plan generation (returns [] when LLM not configured).

        Returns either a sequential list or a PTC dict.  PTC outputs are
        normalised (agent → name) so downstream consumers see a consistent
        shape.
        """
        # P0 — short-circuit on all-error / no_data tool_results. Without
        # this, every "no quote data" path forces the LLM to hallucinate an
        # answer (e.g. "SS = 上证综指"), which then fails L3
        # grounding. A short plain-string fallback is much safer.
        all_no_data = bool(tool_results) and all(
            isinstance(r, dict) and (
                r.get("status") in ("no_data", "error")
                or r.get("error")
                or (r.get("status") == "ok" and not any(
                    r.get(k) for k in ("price", "items", "factors", "alerts", "notes", "rows", "text")))
            )
            for r in tool_results
        )
        if all_no_data and state.symbols:
            sym_label = ", ".join(state.symbols)
            base["summary"] = (
                    f"\u5f53\u524d\u6570\u636e\u6e20\u9053\u672a\u80fd\u8fd4\u56de {sym_label} \u7684\u884c\u60c5\u3002\n"
                    "\n\u53ef\u80fd\u539f\u56e0\uff1a\n"
                    "- \u4e0a\u6e38\u63d0\u4f9b\u8005\u77ed\u65f6\u4e0d\u53ef\u7528 / \u7f51\u7edc\u6296\u52a8\uff1b\n"
                    "- ticker \u5199\u6cd5\u672a\u88ab\u8bc6\u522b\uff08\u8bf7\u68c0\u67e5\u662f\u5426\u4e3a 000001.SS / ^SSEC \u8fd9\u79cd\u6807\u51c6\u5199\u6cd5\uff09\uff1b\n"
                    "- \u76d8\u540e / \u8282\u5047\u65e5\u65e0\u6570\u636e\u3002\n\n"
                    "\u5efa\u8bae\uff1a\u7b49\u5f85\u51e0\u5206\u949f\u540e\u91cd\u8bd5\uff0c\u6216\u68c0\u67e5 ticker \u5199\u6cd5\u3002\u6570\u636e\u672a\u56de\u4e4b\u524d\u6682\u4e0d\u7ed9\u51fa\u65b9\u5411\u3002"
            )
            return base

        if self.llm_factory is None or not self.llm_factory.is_configured():
            return []
        try:
            # §P3-4 — plan generation always runs on the deep model.
            # Plan quality drives downstream routing, so a cheaper
            # quick model would risk producing shallow / misrouted
            # plans.
            #
            # §P3-4 Phase 3 — when the harness wired a per-agent
            # override for the planner slot, use that factory. The
            # override factory ignores ``mode=`` and always uses
            # the dedicated (provider, model) the user picked.
            provider = self._llm_factory_for("planner").make(mode="deep")
            prompt = self._build_plan_prompt(state)
            response = provider.complete_text(prompt=prompt, system=self._PLAN_SYSTEM, temperature=0.0)
            content = getattr(response, "content", response)
            plan = self._parse_plan(content, state)
            # §P3-3+ — hard-enforce intent whitelist. The system prompt
            # encourages multi-source fan-out for B-class queries, so
            # soft hints in the user prompt are routinely ignored. Strip
            # any call whose agent / action is not in the whitelist
            # computed by _build_plan_prompt before normalising.
            plan = self._enforce_intent_whitelist(plan, state)
            # Normalise PTC so the executor doesn't need to (state.plan
            # and the consumed plan stay in sync).
            if isinstance(plan, dict) and plan.get("mode") == "ptc":
                plan = self._normalise_ptc_plan(plan)
            return plan
        except Exception as e:
            LOGGER.warning("LLM plan failed: %s", e)
            return []

    @staticmethod
    def _format_now_cst() -> str:
        """Return current wall-clock as 'YYYY-MM-DD (东八区时间 周X)'.

        Used by plan + synthesize prompts so the LLM sees the real
        current date instead of a frozen literal. Weekday is rendered
        in Chinese to match the surrounding prompt style.
        """
        from datetime import datetime, timezone, timedelta
        cst = timezone(timedelta(hours=8))
        now = datetime.now(cst)
        weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
        return f"{now.strftime('%Y-%m-%d')} (东八区时间 {weekday_cn})"

    _PLAN_SYSTEM = (
        "You are a finance research planner. Reply ONLY with valid JSON. "
        "No commentary, no markdown fences.\n"
        "Question classification (mandatory):\n"
        "- A-class (self-knowledge, no tool needed): current date/weekday, "
        "unit conversion, basic finance terms, definitions. Output an empty "
        "plan [] — SynthesizeNode answers directly using its system prompt.\n"
        "- B-class (market data, fundamentals, news, factors): output a plan "
        "that calls the appropriate agent(s). Use \"agent\": \"data_agent\" "
        "for concurrent quote+fundamentals, \"news_agent\" for news, etc.\n"
        "\n"
        "Two output schemas — pick the one that matches the question:\n"
        "\n"
        "1. SEQUENTIAL — when steps depend on each other (one feeds the next):\n"
        "   [{\"step\": 1, \"agent\": <name>, \"args\": {<dict>}}, ...]\n"
        "\n"
        "2. PTC (Parallel Tool Call) — when 2+ tool calls are independent and "
        "should run concurrently (e.g. quotes for multiple symbols, parallel "
        "news+alpha fetch):\n"
        "   {\"mode\": \"ptc\", \"groups\": [\n"
        "     {\"id\": \"g1\", \"calls\": [\n"
        "       {\"agent\": \"data_agent\", \"args\": {<dict>}}, ...]},\n"
        "     {\"id\": \"g2\", \"calls\": [...], \"depends_on\": [\"g1\"]}\n"
        "   ]}\n"
        "\n"
        "Use PTC when:\n"
        "- 2+ independent data calls (e.g. 3 stock quotes — no data dependency)\n"
        "- Multi-source fan-out (data_agent + news_agent + alpha_agent in parallel)\n"
        "Use SEQUENTIAL when:\n"
        "- Output of step 1 feeds into step 2 (rare for finance data)\n"
        "- Single data call (no parallelism to exploit)\n"
        "\n"
        "Write intents (NOTE / WATCHLIST / ALERT / SCHEDULED create/update/delete) "
        "are dispatched by the CRUD table BEFORE this planner runs — you should "
        "NEVER see them here. If a user request somehow lands at this layer, "
        "emit a data-only heuristic plan and let SynthesizeNode tell the user "
        "to retry.\n"
        "For each call, use \"agent\" (preferred) — the executor maps agents "
        "to their first tool. Available agents and their primary tools are listed "
        "in the user prompt below."
    )

    def _build_plan_prompt(self, state: OrchestratorState) -> str:
        agent_caps = [
            f"- {name}: {self.agent_registry.get(name).description}"
            for name in self.agent_registry.list()
            if self.agent_registry.get(name).description
        ]
        ptc_hint = ""
        if len(state.symbols) >= 2:
            sym_list = ", ".join(state.symbols)
            ptc_hint = (
                f"\nHint: {len(state.symbols)} symbols detected ({sym_list}). "
                "Strongly consider PTC mode with one group containing all "
                "calls — they'll run concurrently and finish in the time of "
                "the slowest single call."
            )
        carry_hint = ""
        if state.carry_symbols:
            carry_hint = (
                f"\nNote: these symbols were carried forward from the "
                f"previous turn (the current message did not name an "
                f"asset explicitly). The user is almost certainly asking "
                f"about them: {state.carry_symbols}.\n"
                f"Prior turn user message: {state.prior_user_msg!r}\n"
            )
        return (
            f"User message: {state.user_message}\n\n"
            f"Current date: {self._format_now_cst()}\n"
            f"Detected symbols (current): {state.symbols}\n"
            f"Detected symbols (carry-forward from previous turn): {state.carry_symbols}\n"
            f"Detected intent: {state.intent.value}\n"
            f"{carry_hint}\n"
            "Available agents:\n" + "\n".join(agent_caps) +
            ptc_hint +
            "\n\nChoose SEQUENTIAL or PTC mode (see system prompt). "
            "For A-class questions (date/weekday/concept), return [] and "
            "the synthesizer will answer directly. If the user refers to "
            "\"this asset\" / \"the company\" / \"加入关注\" without a "
            "ticker, the carry-forward symbols are your anchor."
            "\n\n" + self._WRITE_TOOL_FALLBACK_CATALOG +
            "\n\nIf the user asked for a write (note/alert/watchlist/scheduled), "
            "the CRUD table (already consulted above) handles it — do NOT "
            "re-emit a free-form plan for writes; leave the plan empty so "
            "SynthesizeNode can surface the result."
        )

    _WRITE_TOOL_FALLBACK_CATALOG = (
        # §7.3 #12 — defensive fallback surfaced in _build_plan_prompt.
        "Available write tools (HITL gated — orchestrator auto-prompts "
        "the user for approval; never claim to have created/deleted "
        "without dispatching the matching call):\n"
        "- create_note / update_note / delete_note / delete_notes_for_symbol\n"
        "- create_alert / update_alert / delete_alert / delete_alerts_for_symbol\n"
        "- add_to_watchlist / remove_from_watchlist\n"
        "- create_scheduled_task / update_scheduled_task / delete_scheduled_task / run_scheduled_task\n"
        "Use ``{\"action\": \"<tool_name>\", \"args\": {...}}`` for these."
    )

    _INTENT_AGENT_WHITELIST: dict[str, set[str]] = {
        # Map Intent.value -> allowed agent names. None means: no
        # whitelist (CRUD intents use the dispatch table, not agents).
        "quote":      {"data_agent"},
        "fundamentals": {"data_agent"},
        "history":    {"data_agent"},
        "news":       {"news_agent"},
        "alpha":      {"alpha_agent"},
        "compare":    {"data_agent"},
        "analysis":   {"data_agent"},
        "watchlist":  None, "note": None, "alert": None,
        "scheduled":  None, "run": None, "report": None,
    }

    @staticmethod
    def _is_write_tool(action: str) -> bool:
        """§7.3 #12 — write tools bypass the intent whitelist. CRUD
        dispatch is the primary handler; this lets the LLM emit them
        when a write intent slips past the dispatch (classifier gap).
        """
        return action in {
            "create_note", "update_note", "delete_note",
            "delete_notes_for_symbol",
            "create_alert", "update_alert", "delete_alert",
            "delete_alerts_for_symbol",
            "add_to_watchlist", "remove_from_watchlist",
            "create_scheduled_task", "update_scheduled_task",
            "delete_scheduled_task", "run_scheduled_task",
            "delete_scheduled_tasks_for_symbol",
            "cancel_analysis_run",
        }

    @staticmethod
    def _agent_for_step(step: dict[str, Any]) -> str:
        """Resolve the agent that would handle a plan step.

        Returns the agent name string if the step has ``agent=...``,
        else the action's owning agent (looked up via the inverse of
        ``_resolve_action``'s first-tool mapping). Empty string when
        neither resolves.
        """
        agent = (step.get("agent") or "").strip()
        if agent:
            return agent
        action = (step.get("action") or step.get("name") or "").strip()
        action_to_agent = {
            "get_quote": "data_agent",
            "get_history": "data_agent",
            "get_fundamentals": "data_agent",
            "get_news": "news_agent",
            "list_alpha_factors": "alpha_agent",
        }
        return action_to_agent.get(action, "")

    def _enforce_intent_whitelist(
        self,
        plan: Any,
        state: OrchestratorState,
    ) -> Any:
        """Strip calls outside the intent's agent whitelist.

        Returns the plan unchanged when:

        - the intent has no whitelist (None), or
        - the plan is empty / not a list / not a PTC dict.

        For a sequential list, drops offending steps. For PTC, drops
        offending calls within each group; if every call in a group is
        dropped, the group is removed. Returns the (possibly smaller)
        plan. Logged at INFO so the user / audit log sees what was
        stripped.
        """
        intent_value = (
            state.intent.value
            if hasattr(state.intent, "value") else str(state.intent)
        )
        whitelist = self._INTENT_AGENT_WHITELIST.get(intent_value)
        if whitelist is None:
            return plan
        if not plan:
            return plan
        if isinstance(plan, list):
            kept = []
            for step in plan:
                agent = self._agent_for_step(step)
                # §7.3 #12 — write tools are always allowed through.
                step_action = (step.get("action") or step.get("name") or "").strip()
                if self._is_write_tool(step_action):
                    kept.append(step)
                    continue
                if not agent or agent in whitelist:
                    kept.append(step)
                else:
                    LOGGER.info(
                        "intent-whitelist stripped step agent=%s action=%s intent=%s",
                        agent, step.get("action") or step.get("name"),
                        intent_value,
                    )
            return kept
        if isinstance(plan, dict) and plan.get("mode") == "ptc":
            groups = list(plan.get("groups") or [])
            new_groups = []
            for group in groups:
                calls = list(group.get("calls") or [])
                kept_calls = []
                for call in calls:
                    agent = self._agent_for_step(call)
                    if not agent or agent in whitelist:
                        kept_calls.append(call)
                    else:
                        LOGGER.info(
                            "intent-whitelist stripped ptc call agent=%s action=%s intent=%s",
                            agent, call.get("action") or call.get("name"),
                            intent_value,
                        )
                if kept_calls:
                    new_groups.append({**group, "calls": kept_calls})
            if not new_groups:
                return []
            return {**plan, "groups": new_groups}
        return plan

    @staticmethod
    def _parse_plan(content: str, state: OrchestratorState):
        """Parse LLM plan output into either a sequential list or PTC program.

        Accepts:
          - Sequential: ``[{step, agent|action, args}, ...]``
          - PTC: ``{"mode": "ptc", "groups": [{id, calls: [...], depends_on?}]}``

        Returns ``[]`` on any parse failure so the caller falls back to
        the heuristic plan.
        """
        import json
        import re
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
        try:
            data = json.loads(text)
        except Exception:
            LOGGER.warning("LLM plan returned non-JSON content: %s", content[:120])
            return []
        if isinstance(data, dict) and data.get("mode") == "ptc":
            if Orchestrator._validate_ptc(data):
                return data
            LOGGER.warning("LLM plan returned malformed PTC program: %s", content[:200])
            return []
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        LOGGER.warning("LLM plan returned unexpected shape: %s", content[:120])
        return []

    @staticmethod
    def _validate_ptc(program: dict[str, Any]) -> bool:
        """Best-effort validation of an LLM-emitted PTC program.

        Required: ``mode == "ptc"`` + ``groups`` is a non-empty list of
        dicts each with ``id`` (str), ``calls`` (list of dicts with
        ``agent`` or ``name`` + ``args``), and optional ``depends_on``.
        """
        if not isinstance(program, dict):
            return False
        if program.get("mode") != "ptc":
            return False
        groups = program.get("groups")
        if not isinstance(groups, list) or not groups:
            return False
        seen_ids: set[str] = set()
        for g in groups:
            if not isinstance(g, dict):
                return False
            gid = g.get("id")
            if not isinstance(gid, str) or not gid:
                return False
            if gid in seen_ids:
                return False
            seen_ids.add(gid)
            calls = g.get("calls")
            if not isinstance(calls, list) or not calls:
                return False
            for c in calls:
                if not isinstance(c, dict):
                    return False
                if not (c.get("agent") or c.get("name")):
                    return False
                if not isinstance(c.get("args", {}), dict):
                    return False
            deps = g.get("depends_on")
            if deps is not None:
                if not isinstance(deps, list):
                    return False
                for d in deps:
                    if not isinstance(d, str) or not d:
                        return False
        return True

    # §Step 34 — citation retry budget. When the first synthesize pass
    # fails the citation contract (no 来源 block) and we have data-
    # bearing tool results, retry up to ``_CITATION_RETRY_LIMIT`` times
    # with an extra "STRICT CITATION REQUIRED" hint appended to the
    # prompt. We bail out after the budget and return whatever the
    # last attempt produced — the L3 judge still surfaces a low
    # citation_score so the UI badge can render correctly.
    _CITATION_RETRY_LIMIT = 1
    _CITATION_RETRY_HINT = (
        "\n\nIMPORTANT (retry hint): The previous answer lacked the "
        "required 来源 / source block. List EVERY tool that contributed "
        'data as either a blockquote ("> 来源: get_quote, get_news") '
        'or a "## 来源" section. Do NOT use forward-looking words '
        '("预计 / 估计 / estimate / target price") unless the tool '
        "returned that data — otherwise stick to observed numbers only."
    )

    # §P2 — turn-level synth retry. When the L3 judge fails on
    # ``claim_audit`` (numbers in the prose not backed by tools), the
    # orchestrator can re-trigger ``_llm_synthesize`` once with a
    # specific directive naming the unsupported claims. Default 1
    # keeps total attempts ≤ 1 initial + 1 intra + 1 turn-level.
    _SYNTH_TURN_RETRY_LIMIT = 1
    _SYNTH_TURN_RETRY_HINT_TMPL = (
        "\n\nIMPORTANT (L3 retry hint): The previous answer was "
        "flagged by claim_audit for the following unsupported numbers "
        "or forward claims: {unsupported}.\n"
        "- If these numbers are NOT in the tool results, REMOVE them "
        "or replace them with the tool-returned values.\n"
        "- If they came from general knowledge, mark them with "
        "\"参考\" / \"常识\" so the L3 judge can "
        "down-weight them instead of failing.\n"
        "- Do NOT add new numbers that aren't in the tool results."
    )

    async def _llm_synthesize(self, state: OrchestratorState) -> Any:
        """Real LLM synthesis when configured; fallback dict otherwise.

        Step 34 — citation retry loop. If the first attempt produces
        an answer that the citation detector flags (no 来源 block +
        data tools were used), we append a strict hint and retry
        once. The retry budget defaults to 1; bump
        ``_CITATION_RETRY_LIMIT`` for production tuning.
        """
        base = {
            "intent": state.intent.value,
            "symbols": state.symbols,
            "results": state.tool_results,
        }
        if self.llm_factory is None or not self.llm_factory.is_configured():
            base["summary"] = "(LLM not configured — returning raw tool results)"
            return base
        try:
            from tradingagents.agent_harness.verification.citations import (
                citation_score as _citation_score,
            )
            from tradingagents.agent_harness.verification.claim_audit import (
                has_forward_claim as _has_forward_claim,
            )
        except Exception:
            _citation_score = None
            _has_forward_claim = None
        # §P3-4 — synthesise the final user-facing answer on the deep
        # model. The synthesised text is what the user actually sees,
        # so cost/quality tradeoff favours depth here.
        #
        # §P3-4 Phase 3 — when the harness wired a per-agent
        # override for the synthesizer slot, use that factory
        # instead of the main one.
        provider = self._llm_factory_for("synthesizer").make(mode="deep")
        prompt = self._build_synthesize_prompt(state)
        # §P2 — turn-level L3 retry directive. If the L3 judge
        # flagged the previous attempt for unsupported numbers, the
        # orchestrator stashed a specific directive here. We consume
        # it on the first attempt and clear it so intra-synth retries
        # use the citation retry hint instead.
        l3_directive = getattr(state, "synth_retry_directive", None)
        if l3_directive:
            prompt = prompt + "\n\n" + l3_directive
            state.synth_retry_directive = None
        last_content = None
        # Identify data tools used so we can decide whether citation
        # retry is even meaningful (skip retry on trivial CRUD acks).
        used_data_tools = [
            r.get("name") or r.get("tool") or ""
            for r in (state.tool_results or [])
            if isinstance(r, dict)
        ]
        used_data_tools = [t for t in used_data_tools if t]
        from tradingagents.agent_harness.verification.citations import DATA_TOOLS
        has_data_tools = any(
            t.lower() in DATA_TOOLS for t in used_data_tools
        )
        attempts_left = self._CITATION_RETRY_LIMIT if has_data_tools else 0
        while True:
            try:
                response = provider.complete_text(
                    prompt=prompt, system=self._SYNTH_SYSTEM, temperature=0.0,
                )
                content = getattr(response, "content", response)
            except Exception as e:
                LOGGER.warning("LLM synthesize failed: %s", e)
                # If we have a previous attempt, keep that instead of
                # falling back to the generic placeholder.
                if last_content is not None:
                    content = last_content
                else:
                    base["summary"] = "(LLM synthesize failed — returning raw tool results)"
                    return base
            last_content = content
            # Decide whether to retry.
            if attempts_left <= 0 or _citation_score is None:
                break
            cite = _citation_score(content, used_data_tools)
            # Retry when citation is missing OR the answer uses
            # forward-looking language ("预计 / 估计 / target price")
            # — both signals mean the LLM tried to make a claim the
            # tool data didn\'t back up.
            has_forward = (
                _has_forward_claim is not None
                and _has_forward_claim(content)
            )
            needs_retry = cite < 0.4 or has_forward
            if not needs_retry:
                break
            attempts_left -= 1
            LOGGER.info(
                "synth retry due to citation score %.2f < 0.4 (tools=%s)",
                cite, used_data_tools,
            )
            prompt = prompt + self._CITATION_RETRY_HINT
        base["summary"] = last_content
        return base

    _SYNTH_SYSTEM = (
        "You are a finance research assistant. Synthesize tool results into "
        "a concise, accurate answer in the user\u2019s language.\n"
        "\n"
        "Output structure (use markdown headings):\n"
        "Style:\n"
        "- Be concise: prefer 3\u20136 short paragraphs over long bulleted dumps.\n"
        "- Use markdown headings ONLY when the answer has clear sections. For trivial lookups (single quote, single note) write a plain sentence, no headings.\n"
        "- Use the user\u2019s language for any natural-language answer.\n"
        "\n"
        "Grounding rules:\n"
        "- Numbers must come from tool results. If a tool returned null / "
        "\"[stub]\" / an error, surface it as missing rather than inventing "
        "an estimate.\n"
        "- Brief historical references (e.g. \"2021 年高点约 53 元\") "
        "are allowed if marked \"参考\" / \"常识\" — the L3 "
        "judge down-weights them but does not fail the answer.\n"
        "\n"
        "Citation contract (Step 22 P1):\n"
        "- When the answer cites numbers / news / events from tool results, "
        "end the answer with a \"\u8d44\u6599\u6765\u6e90\" section.\n"
        "- Format: either a blockquote (\"> \u8d44\u6599\u6765\u6e90: get_quote (600036.SS) \u00b7 get_news (600036.SS)\") "
        "or a heading (\"## \u8d44\u6599\u6765\u6e90\") followed by one \"> tool_name (symbol)\" row per tool that contributed data.\n"
        "- The L3 judge reads this block; answers without it are down-weighted "
        "even when the numbers are correct \u2014 verifiability matters.\n"
        "- Trivial CRUD acks (\"Delete a single note\") do NOT need a citation block.\n"
    )


    def _build_l3_reference_block(self, state: OrchestratorState) -> str:
        """§Step 21 — surface prior session discussions to the LLM.

        Reads from ``self.memory.l3`` (cross-session knowledge cache)
        and renders the last ``_L3_REFERENCE_LIMIT`` entries into a
        short prose paragraph the synthesizer can anchor on.

        Returns empty string when no L3 is wired, when memory is None,
        or when no relevant references exist.
        """
        if getattr(self, "memory", None) is None:
            return ""
        try:
            l3 = self.memory.l3
        except AttributeError:
            return ""
        if l3 is None:
            return ""
        try:
            entries = l3.list(prefix="discussions:")
        except Exception as e:
            LOGGER.debug("L3 references read failed: %s", e)
            return ""
        # Filter out expired and current-session entries (we don't want
        # to surface the *current* turn's context as "history").
        current_sid = getattr(state, "session_id", None)
        relevant = []
        for e in entries:
            if e.session_id == current_sid:
                continue
            value = e.value if isinstance(e.value, dict) else {}
            relevant.append(value)
        if not relevant:
            return ""
        relevant = relevant[-self._L3_REFERENCE_LIMIT:]
        lines = []
        for v in relevant:
            syms = v.get("symbols") or []
            summary = v.get("summary") or v.get("user_msg") or ""
            sym_str = ", ".join(syms) if syms else "(no symbols)"
            lines.append(f"- session {v.get("session_id", "?")[:14]}: {sym_str} · {summary[:120]}")
        return "\n".join(lines)

    _L3_REFERENCE_LIMIT = 5

    def _build_synthesize_prompt(self, state: OrchestratorState) -> str:
        import json as _json
        # §Step 21 — load cross-session L3 references so the LLM can
        # scope its answer to prior discussion context. Without this,
        # the synthesizer sees a green-field prompt every turn and
        # can't tell whether the current symbols match what the user
        # was looking at moments ago (different session).
        l3_block = self._build_l3_reference_block(state)
        results_dump = _json.dumps(state.tool_results, ensure_ascii=False, default=str)[:6000]
        # §P3-3+ — surface focus-asset hint to the synthesizer. Without
        # this, list_notes / list_alerts (which return ALL records) are
        # interpreted as "user didn't specify a ticker" and the
        # synthesizer asks the user to clarify even when the carry-
        # forward symbols are obviously the focus. Inject both
        # ``state.symbols`` (current turn) and ``state.carry_symbols``
        # (previous-turn anchor) so the synthesizer can scope the
        # results to the asset the user actually meant.
        intent_value = (
            state.intent.value
            if hasattr(state.intent, "value") else str(state.intent)
        )
        focus_lines: list[str] = []
        if state.symbols:
            focus_lines.append(
                f"- Current-turn symbols: {state.symbols} (explicit in user message)"
            )
        if state.carry_symbols:
            focus_lines.append(
                f"- Carry-forward symbols: {state.carry_symbols} "
                f"(previous turn's asset; current message has no explicit ticker)"
            )
        if not focus_lines:
            focus_lines.append("- No symbols detected; tool results cover all assets")
        focus_block = "\n".join(focus_lines)
        # §Step 21 — splice L3 history into the prompt between intent
        # declaration and focus-block. When the user message names a
        # ticker already discussed in another session, the LLM can
        # avoid hallucinating context it should look up. The block is
        # rendered even when empty so the LLM knows no history was
        # available (rather than silently dropping the section).
        if l3_block:
            l3_section = f"\nL3 历史参考 (跨 session 长期记忆):\n{l3_block}\n"
        else:
            l3_section = "\nL3 历史参考: (无)\n"
        # §P3-3+ — detect pending_approval in tool_results so the
        # synthesizer can tell the user the UI is handling the
        # confirmation, not to type it as a chat message.
        pending_approvals = [
            r for r in (state.tool_results or [])
            if isinstance(r, dict)
            and isinstance(r.get("result"), dict)
            and r["result"].get("status") == "pending_approval"
        ]
        pending_note = ""
        if pending_approvals:
            tools = sorted({
                r["result"].get("tool_name", "?")
                for r in pending_approvals
            })
            pending_note = (
                "\n\nIMPORTANT: One or more write tools returned "
                "``pending_approval``. The UI is ALREADY showing a "
                "confirmation dialog (centered modal) with 批准/拒绝 "
                "buttons. Tell the user explicitly: \"\u9875\u9762\u5df2 "
                "\u5f39\u51fa\u5ba1\u6279\u5bf9\u8bdd\u6846\uff0c\u8bf7\u70b9\u51fb "
                "\u6279\u51c6 \u6216 \u62d2\u7edd \u6309\u94ae\u3002\" Do NOT ask "
                "the user to type \"\u786e\u8ba4\u5220\u9664\" or similar "
                "phrases \u2014 the confirmation is a UI action, not a chat "
                "message. The pending tools are: " + ", ".join(tools) + "."
            )
        return (
            f"Current date: {self._format_now_cst()}\n\n"
            f"User message: {state.user_message}\n\n"
            f"Detected intent: {intent_value}\n"
            f"{l3_section}"
            f"Focus assets for this turn:\n{focus_block}\n\n"
            f"Tool results: {results_dump}{pending_note}\n\n"
            "Follow the structure in your system prompt: "
            "\u6570\u636e\u4e8b\u5b9e / \u884c\u4e3a\u9762\u89c2\u5bdf / "
            "\u65b9\u5411\u6027\u5efa\u8bae. "
            "For A-class questions (date/weekday/concept) where tool_results "
            "is empty, answer directly from your own knowledge. "
            "When tool results are for ALL assets (e.g. list_notes / "
            "list_alerts return global lists) but the focus-asset hint "
            "names a specific symbol, scope the \u6570\u636e\u4e8b\u5b9e "
            "section to that symbol; mention other assets only as "
            "\"out of scope\". Do NOT ask the user to clarify the ticker "
            "when the focus-asset hint already names it \u2014 that is the answer."
            + ("" if not pending_approvals else
               "\n\nFor pending_approval results: just acknowledge "
               "the dialog is showing \u2014 do NOT enumerate the data "
               "fields or repeat the args. The user will click \u6279\u51c6 "
               "(Approve) or \u62d2\u7edd (Reject) in the modal."
              )
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _dump(obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        return obj

    @staticmethod
    def _resolve_action(step: dict[str, Any]) -> tuple[str, str]:
        """Resolve a plan step to ``(action_name, agent_name)``.

        Spec plans use ``action=<tool_name>`` (canonical). LLMs that ignore
        this convention often emit ``agent=<sub_agent_name>`` instead — for
        those we look up the agent in the registry and pick its first tool
        as the action (N122 fix, 2026-09-14).

        Returns
        -------
        (action, agent) tuple — either may be empty.
        """
        action = step.get("action") or ""
        agent_name = step.get("agent") or ""
        if action or not agent_name:
            return action, agent_name
        # The orchestrator doesn't own the registry directly; the agent name
        # itself is the lookup key (we don't need the registry instance here
        # because we only need to know the agent's tool list — and the LLM
        # already passes the symbol/args, so the executor picks the tool
        # by walking the agent's ``tools`` list).
        # Per-agent first-tool mapping (matches P5 §4.1):
        agent_to_first_tool = {
            "data_agent": "get_quote",
            "alpha_agent": "list_alpha_factors",
            "news_agent": "get_news",
            # planner / verifier / synthesizer: no tool, handled outside
        }
        first_tool = agent_to_first_tool.get(agent_name, "")
        return first_tool, agent_name


def _format_trivial_summary(tool_results):
    """Render a one-line summary for a batch of CRUD acks.

    Pure templating — never invokes the LLM. Used by Orchestrator._synthesize
    as a fast-path when every tool result is a trivial CRUD ack (no real
    data to analyse, just a "yes it was created / deleted / updated" string).
    """
    if not tool_results:
        return ""
    parts = []
    pending = False
    for r in tool_results:
        if not isinstance(r, dict):
            return ""
        status = r.get("status")
        raw = r.get("raw") or ""
        symbol = r.get("symbol")
        if status == "created" and "NOTE_CREATED" in raw:
            parts.append(f"笔记已保存{symbol_part(symbol)}")
        elif status == "updated" and "NOTE_UPDATED" in raw:
            parts.append(f"笔记已更新{symbol_part(symbol)}")
        elif status == "deleted" and "NOTE_DELETED" in raw:
            parts.append(f"笔记已删除{symbol_part(symbol)}")
        elif status == "created" and "ALERT_CREATED" in raw:
            parts.append(f"告警已创建{symbol_part(symbol)}")
        elif status == "deleted" and "ALERT_DELETED" in raw:
            parts.append(f"告警已删除{symbol_part(symbol)}")
        elif status == "duplicate":
            parts.append(f"{symbol or ''} 已在关注列表中,无需重复添加".strip())
        elif status == "pending_approval":
            pending = True
        elif status == "ok" and "PREFERENCE_UPDATED" in raw:
            parts.append("偏好已更新")
        elif status == "ok" and "added" in raw.lower():
            parts.append(f"{symbol or ''} 已加入关注".strip())
        elif status == "ok":
            parts.append("操作成功")
    if pending:
        parts.append("等待你确认")
    return "\n".join(parts)


def symbol_part(symbol):
    """Return 'symbol' or '' for templating."""
    return f" ({symbol})" if symbol else ""
