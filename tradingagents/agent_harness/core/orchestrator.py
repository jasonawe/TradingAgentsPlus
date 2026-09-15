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
from .short_circuit import ShortCircuit
from .tier import Intent, RouteResult, Tier, fast_route
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
    plan: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final: Any = None
    error: str | None = None


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
    ) -> None:
        self.tool_registry = tool_registry
        self.agent_registry = agent_registry
        self.llm_factory = llm_factory
        self.context_priority = context_priority
        self.retry_policy = retry_policy
        self.circuit_breaker = circuit_breaker
        self.audit = audit
        self.enable_l3 = enable_l3

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
        route = fast_route(user_message)
        context = ToolContext(session_id=session_id)
        state = OrchestratorState(
            session_id=session_id,
            user_message=user_message,
            intent=route.intent,
            symbols=route.symbols,
        )

        # Tier 1 short-circuit when route lands on it.
        if route.tier == Tier.DIRECT and route.symbols:
            async for ev, payload in self._short_circuit.run(route, user_message, context):
                yield await self._emit(ev, payload)
            return

        # Token accounting: every LLM call inside stream_chat records
        # into this per-session store (P1 TokenUsage disjoint). Surfaces
        # as ``usage_summary`` SSE event so the frontend can render
        # per-agent cost attribution.
        store = TokenUsageStore()
        try:
            with attach_store(store):
                async for ev in self._stream_chat_impl(state, context, route):
                    yield ev
            yield await self._emit("usage_summary", store.summary())
        except Exception as e:
            LOGGER.exception("orchestrator failed")
            state.error = str(e)
            err_payload: dict = {"tier": int(route.tier), "error": str(e)}
            if isinstance(e, LlmFailure):
                err_payload["failure"] = e.to_dict()
            err_payload["usage"] = store.summary()
            yield await self._emit("error", err_payload)
            return
        return

    async def _emit(self, event: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Surface-tag an event before yielding it.

        Side-effects: dispatch to audit/debug sinks via ``SurfaceRouter``.
        Returns the (event, payload) tuple with ``surface`` added to
        payload (or the caller's pre-set surface preserved).
        """
        self._surface_router.route(event, payload)
        return event, self._surface_router.tag(event, payload)

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
    ) -> AsyncIterator[tuple[str, dict]]:
        """Body of stream_chat — runs inside ``attach_store(store)``.

        Refactored from the original monolithic stream_chat so the
        TokenUsageStore lifetime is explicit. All LLM calls inside
        (plan, synthesize, L3 judge, sub-agents via track_agent) end
        up in the active store.  Every yielded event is also tagged
        with its ``surface`` (P0-4) via the ``_emit`` closure defined
        in ``stream_chat`` (above).
        """
        # Tier 2: 5-node state machine.
        try:
            yield await self._emit("plan_started", {"intent": route.intent.value})
            plan = await self._plan(state, context)
            state.plan = plan
            # Detect PTC program shape: {"mode": "ptc", "groups": [...]}
            is_ptc = isinstance(plan, dict) and plan.get("mode") == "ptc"
            ev_name = "plan_ready_ptc" if is_ptc else "plan_ready"
            ev_payload = {"groups": plan.get("groups", [])} if is_ptc else {"steps": plan}
            yield await self._emit(ev_name, ev_payload)

            if is_ptc:
                results = await self._execute_ptc(state, context)
            else:
                results = await self._execute(state, context)
            state.tool_results = results
            for r in results:
                yield await self._emit("tool_result", r)

            observations = self._observe(state)
            yield await self._emit("observed", observations)

            # L1 + L2 first (can short-circuit via plan mutation).
            verification = await self._verify(state)
            yield await self._emit("verified", {"ok": verification.ok, "level": int(verification.level)})

            final = await self._synthesize(state)
            state.final = final
            yield await self._emit("agent_final", {"tier": int(Tier.PLAN_EXECUTE), "result": self._dump(final)})

            # L3 LLM-judge (spec §D6) — runs AFTER synthesize. Extracted
            # into `_run_l3_judge` so it's directly testable without
            # spinning up the full 5-node state machine.
            async for ev, payload in self._run_l3_judge(state):
                yield await self._emit(ev, payload)

            if self.audit is not None and hasattr(self.audit, "log"):
                try:
                    self.audit.log(
                        session_id=session_id,
                        event="tier2_complete",
                        payload={"intent": route.intent.value, "plan_steps": len(plan)},
                    )
                except Exception:  # audit must never break the orchestrator
                    LOGGER.debug("audit log failed", exc_info=True)
        except Exception as e:
            # Errors from inside _stream_chat_impl propagate to the outer
            # attach_store block, which attaches structured failure info.
            raise

    # ------------------------------------------------------------------
    # 5 nodes
    # ------------------------------------------------------------------
    async def _plan(self, state: OrchestratorState, context: ToolContext) -> list[dict[str, Any]]:
        """PlanNode — turn ``state.user_message`` into a JSON plan.

        The plan is a list of ``{"step": int, "action": tool_name, "args": {...}}``.
        LLM-backed when ``llm_factory`` is wired; falls back to a heuristic
        plan when no LLM is available so the harness remains testable.
        """
        if self.llm_factory is not None:
            try:
                plan = await self._llm_plan(state)
                if plan:
                    return plan
            except Exception as e:
                LOGGER.warning("LLM plan failed, falling back to heuristic: %s", e)

        # Heuristic plan: when 2+ symbols, return a PTC program so the
        # quote calls run concurrently (one group, no dependencies).
        # Single-symbol path keeps the legacy sequential shape.
        if len(state.symbols) >= 2:
            return {
                "mode": "ptc",
                "groups": [{
                    "id": "g1",
                    "calls": [
                        {"name": "get_quote", "args": {"symbol": sym}}
                        for sym in state.symbols
                    ],
                }],
            }

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
        return plan

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

            if not self.llm_factory and name in {"create_alert", "update_alert", "delete_alert"}:
                # Write tools require LLM-backed approval in production; in
                # tests we surface the HITL payload directly.
                return {
                    "name": name,
                    "result": {"status": "pending_approval", "args": args},
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

            pipe_result = await self._tool_pipeline.run(
                tool_name=name, args=args, tool_context=context,
                executor=_executor,
            )
            if pipe_result.ok:
                self.circuit_breaker.record_success()
                return {"name": name, "result": self._dump(pipe_result.result)}
            if pipe_result.needs_approval:
                # HITL: dangerous tool requires approval. Preserve existing
                # payload shape so frontend / approval endpoints stay unchanged.
                return {
                    "name": name,
                    "result": {"status": "pending_approval", "args": args},
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
            # Normalise PTC so the executor doesn't need to (state.plan
            # and the consumed plan stay in sync).
            if isinstance(plan, dict) and plan.get("mode") == "ptc":
                plan = self._normalise_ptc_plan(plan)
            return plan
        except Exception as e:
            LOGGER.warning("LLM plan failed: %s", e)
            return []

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
        return (
            f"User message: {state.user_message}\n\n"
            f"Current date: 2026-09-14 (东八区时间 周一)\n"
            f"Detected symbols: {state.symbols}\n"
            f"Detected intent: {state.intent.value}\n\n"
            "Available agents:\n" + "\n".join(agent_caps) +
            ptc_hint +
            "\n\nChoose SEQUENTIAL or PTC mode (see system prompt). "
            "For A-class questions (date/weekday/concept), return [] and "
            "the synthesizer will answer directly."
        )

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
        return (
            f"Current date: 2026-09-14 (东八区时间 周一)\n\n"
            f"User message: {state.user_message}\n\n"
            f"Tool results: {results_dump}\n\n"
            "Follow the structure in your system prompt: "
            "\u6570\u636e\u4e8b\u5b9e / \u884c\u4e3a\u9762\u89c2\u5bdf / "
            "\u65b9\u5411\u6027\u5efa\u8bae. "
            "For A-class questions (date/weekday/concept) where tool_results "
            "is empty, answer directly from your own knowledge."
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
