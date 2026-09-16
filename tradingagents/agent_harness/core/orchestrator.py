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
    """
    sym = _focused_symbol(state)
    return {"symbol": sym} if sym else {}


def _list_alerts_args(state: Any) -> dict[str, Any]:
    """list_alerts: same scoping as ``_list_notes_args``."""
    sym = _focused_symbol(state)
    return {"symbol": sym} if sym else {}


def _list_reports_args(state: Any) -> dict[str, Any]:
    """list_reports: scope to the focused symbol when one is known
    (explicit ``state.symbols[0]`` or carry-forward ``state.carry_symbols[0]``).

    Empty args when no focused symbol — preserves "all reports" behaviour.
    """
    sym = _focused_symbol(state)
    return {"symbol": sym} if sym else {}


def _list_runs_args(state: Any) -> dict[str, Any]:
    """list_runs: scope to the focused symbol when one is known.

    Same carry-forward semantics as :func:`_list_notes_args`; empty
    args when no focused symbol is set so ``manager.list_runs`` is
    used (unfiltered) instead of ``list_runs_for_ticker``.
    """
    sym = _focused_symbol(state)
    return {"symbol": sym} if sym else {}


def _list_scheduled_tasks_args(state: Any) -> dict[str, Any]:
    """list_scheduled_tasks: scope to the focused symbol when one is known.

    Each scheduler job carries a ``symbol`` field; the bridge tool
    filters ``items`` in Python when ``symbol`` is non-empty.
    """
    sym = _focused_symbol(state)
    return {"symbol": sym} if sym else {}


def _note_create_args(state: Any) -> dict[str, Any]:
    """create_note: pull (title, content) from the user message. For now
    uses the raw message as the body and an empty title; LLM-backed plan
    generation can refine these later."""
    msg = state.user_message or ""
    return {"symbol": "", "body_md": msg, "asset_type": "stock"}


def _note_id_args(state: Any) -> dict[str, Any]:
    """update_note / delete_note: extract the first note_id-looking token
    (``note-xxx`` or just digits) from the user message. Falls back to an
    empty string so the tool reports its own error."""
    import re as _re
    msg = state.user_message or ""
    m = _re.search(r"note-[A-Za-z0-9_-]+", msg)
    if m:
        return {"note_id": m.group(0)}
    return {"note_id": ""}


def _alert_create_args(state: Any) -> dict[str, Any]:
    """create_alert: pull (symbol, kind, params) from state. Default to a
    simple price alert on the first carry-forward symbol."""
    syms = list(state.symbols or [])
    sym = syms[0] if syms else ""
    return {
        "symbol": sym,
        "kind": "price",
        "params": {"threshold": 0.0},
        "asset_type": "stock",
    }


def _alert_id_args(state: Any) -> dict[str, Any]:
    import re as _re
    msg = state.user_message or ""
    m = _re.search(r"alert-[A-Za-z0-9_-]+", msg)
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


def _scheduled_create_args(state: Any) -> dict[str, Any]:
    """create_scheduled_task: build minimal args from the message. Real
    cron / symbol extraction is left to LLM-backed plan refinement."""
    syms = list(state.symbols or [])
    return {
        "symbol": syms[0] if syms else "",
        "asset_type": "stock",
        "cron_expression": "0 9 * * 1-5",  # weekdays 09:00 — sensible default
        "timezone": "Asia/Shanghai",
    }


def _scheduled_id_args(state: Any) -> dict[str, Any]:
    import re as _re
    msg = state.user_message or ""
    m = _re.search(r"job-[A-Za-z0-9_-]+", msg)
    if m:
        return {"job_id": m.group(0)}
    return {"job_id": ""}


def _run_create_args(state: Any) -> dict[str, Any]:
    syms = list(state.symbols or [])
    return {
        "symbol": syms[0] if syms else "",
        "trade_date": "",
        "asset_type": "stock",
        "research_depth": 1,
    }


def _run_id_args(state: Any) -> dict[str, Any]:
    import re as _re
    msg = state.user_message or ""
    m = _re.search(r"run-[A-Za-z0-9_-]+", msg)
    if m:
        return {"run_id": m.group(0)}
    return {"run_id": ""}


def _report_id_args(state: Any) -> dict[str, Any]:
    import re as _re
    msg = state.user_message or ""
    m = _re.search(r"report-[A-Za-z0-9_-]+", msg)
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
    ) -> None:
        self.tool_registry = tool_registry
        self.agent_registry = agent_registry
        self.llm_factory = llm_factory
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
        from tradingagents.agent_harness.core.plan_template import PlanTemplateCache
        self.plan_cache: PlanTemplateCache = PlanTemplateCache()

    # ------------------------------------------------------------------
    # §P3-2 — per-turn memory helpers
    # ------------------------------------------------------------------
    _SESSION_CTX_KEY = "__session_ctx__"

    def _load_session_context(self, session_id: str) -> dict:
        """Read previous-turn metadata from L2 (symbols / intent / user_msg).

        Used at the top of :meth:`stream_chat` to carry forward symbols
        when the current user_message has none (e.g. "加入关注" without
        naming a ticker). Returns empty dict on no memory / no prior turn.
        """
        if self.memory is None or not session_id:
            return {"symbols": [], "intent": None, "user_msg": None}
        try:
            entry = self.memory.l2.get(self._SESSION_CTX_KEY, session_id=session_id)
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
            self.memory.l2.set(
                self._SESSION_CTX_KEY,
                {
                    "symbols": list(symbols or []),
                    "intent": getattr(intent, "value", str(intent) if intent else None),
                    "user_msg": (user_msg or "")[:200],
                },
                session_id=session_id,
            )
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
        route, op_from_route = fast_route_with_op(user_message)
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

        # §P3-2 — symbol carry-forward: if the current message has no
        # ticker (e.g. "加入关注", "看一下估值", "分析这家"),") pull the
        # last turn's symbols out of L2 so the planner / tier routing can
        # still act on the implicit asset reference.
        session_ctx = self._load_session_context(session_id)
        carry_symbols: list[str] = []
        if not route.symbols and session_ctx.get("symbols"):
            carry_symbols = list(session_ctx["symbols"])
            LOGGER.info(
                "carry-forward symbols from previous turn: %s (session=%s)",
                carry_symbols, session_id,
            )
        effective_symbols = list(route.symbols) + [
            s for s in carry_symbols if s not in route.symbols
        ]

        state = OrchestratorState(
            session_id=session_id,
            user_message=user_message,
            intent=route.intent,
            symbols=effective_symbols,
            carry_symbols=carry_symbols,
            prior_user_msg=session_ctx.get("user_msg"),
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
        if route.tier == Tier.DIRECT and route.symbols:
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
            async for ev, payload in self._short_circuit.run(route, user_message, context):
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
            yield await _emit("agent_final", {"tier": int(Tier.PLAN_EXECUTE), "result": self._dump(final)})

            # L3 LLM-judge (spec §D6) — runs AFTER synthesize. Extracted
            # into `_run_l3_judge` so it's directly testable without
            # spinning up the full 5-node state machine.
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
        """
        if state.op is None or state.intent is None:
            return []
        key = (state.intent, state.op)
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
        # §P3-3+ — multi-intent CRUD dispatch BEFORE the LLM plan.
        # The LLM doesn't know about list_notes / list_alerts / etc.
        # (those are CRUD tools, not in the standard agent list), so
        # for multi-intent CRUD queries ("看一下笔记和告警") the LLM
        # would otherwise fall back to data_agent.get_quote and the
        # user gets the wrong data. Check multi-CRUD first; if it
        # resolves, return immediately so the LLM never sees this turn.
        if getattr(state, "extra_crud_dispatch", None):
            multi_plan = self._multi_crud_plan(state)
            if multi_plan is not None:
                self.plan_cache.put(state.user_message, multi_plan)
                return multi_plan
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
        # (multi-CRUD is now checked BEFORE the LLM plan; see top of _plan)
        # §P3-3 — single CRUD dispatch (replaces the §P3-2 WATCHLIST
        # special branch). If (state.intent, state.op) maps to a tool,
        # return a one-step plan so the synthesizer reports the actual
        # write result. Otherwise leave the data-only plan as-is.
        crud_plan = self._crud_plan_for_state(state)
        if crud_plan:
            self.plan_cache.put(state.user_message, crud_plan)
            return crud_plan
        # Cache the heuristic plan so the next identical query reuses it
        # without going through _plan again.
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
                return {"name": name, "result": self._dump(pipe_result.result)}
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
        """SynthesizeNode — turn tool results into a final answer."""
        if self.llm_factory is not None:
            try:
                return await self._llm_synthesize(state)
            except Exception as e:
                LOGGER.warning("LLM synthesize failed, returning raw tool_results: %s", e)
        return {
            "intent": state.intent.value,
            "symbols": state.symbols,
            "results": state.tool_results,
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
        if self.llm_factory is None or not self.llm_factory.is_configured():
            return []
        try:
            provider = self.llm_factory.make()
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

    async def _llm_synthesize(self, state: OrchestratorState) -> Any:
        """Real LLM synthesis when configured; fallback dict otherwise."""
        base = {
            "intent": state.intent.value,
            "symbols": state.symbols,
            "results": state.tool_results,
        }
        if self.llm_factory is None or not self.llm_factory.is_configured():
            base["summary"] = "(LLM not configured — returning raw tool results)"
            return base
        try:
            provider = self.llm_factory.make()
            prompt = self._build_synthesize_prompt(state)
            response = provider.complete_text(
                prompt=prompt, system=self._SYNTH_SYSTEM, temperature=0.0
            )
            content = getattr(response, "content", response)
            base["summary"] = content
            return base
        except Exception as e:
            LOGGER.warning("LLM synthesize failed: %s", e)
            base["summary"] = "(LLM synthesize failed — returning raw tool results)"
            return base

    _SYNTH_SYSTEM = (
        "You are a finance research assistant. Synthesize tool results into "
        "a concise, accurate answer in the user\u2019s language.\n"
        "\n"
        "Output structure (use markdown headings):\n"
        "1. **数据事实** — ONLY data points actually returned "
        "by tools. Quote numbers verbatim; mark missing fields as \"—\".\n"
        "2. **行为面观察** — technical / flow-based reading "
        "(e.g. \"量比 1.22 + 换手 6.9% → 资金接继\"). "
        "Allowed to use finance common knowledge, but mark inferences with "
        "\"基于××推断\" so the L3 judge can verify.\n"
        "3. **方向性建议** — a concrete stance "
        "(观望 / 跳过 / 关注支撑位) with a one-line "
        "rationale + risk note. NEVER output \"不能给出结论\" "
        "— a direction is required even when data is partial.\n"
        "\n"
        "Grounding rules:\n"
        "- Numbers must come from tool results. If a tool returned null / "
        "\"[stub]\" / an error, surface it as missing rather than inventing "
        "an estimate.\n"
        "- Brief historical references (e.g. \"2021 年高点约 53 元\") "
        "are allowed if marked \"参考\" / \"常识\" — the L3 "
        "judge down-weights them but does not fail the answer."
    )

    def _build_synthesize_prompt(self, state: OrchestratorState) -> str:
        import json as _json
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
        return (
            f"Current date: {self._format_now_cst()}\n\n"
            f"User message: {state.user_message}\n\n"
            f"Detected intent: {intent_value}\n"
            f"Focus assets for this turn:\n{focus_block}\n\n"
            f"Tool results: {results_dump}\n\n"
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
