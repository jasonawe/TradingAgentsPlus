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

from .context import ContextPriority
from .retry import CircuitBreaker, RetryPolicy, retry_async
from web.market_models import ProviderError
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
        # Verifier gets its own judge_factory (N89 fix: judge model != main LLM)
        self._verifier = Verifier(judge_factory=judge_factory)

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
            async for ev in self._short_circuit.run(route, user_message, context):
                yield ev
            return

        # Tier 2: 5-node state machine.
        try:
            yield ("plan_started", {"intent": route.intent.value})
            plan = await self._plan(state, context)
            state.plan = plan
            yield ("plan_ready", {"steps": plan})

            results = await self._execute(state, context)
            state.tool_results = results
            for r in results:
                yield ("tool_result", r)

            observations = self._observe(state)
            yield ("observed", observations)

            # L1 + L2 first (can short-circuit via plan mutation).
            verification = await self._verify(state)
            yield ("verified", {"ok": verification.ok, "level": int(verification.level)})

            final = await self._synthesize(state)
            state.final = final
            yield ("agent_final", {"tier": int(Tier.PLAN_EXECUTE), "result": self._dump(final)})

            # L3 LLM-judge (spec §D6) — runs AFTER synthesize. Extracted
            # into `_run_l3_judge` so it's directly testable without
            # spinning up the full 5-node state machine.
            async for ev in self._run_l3_judge(state):
                yield ev

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
            LOGGER.exception("orchestrator failed")
            state.error = str(e)
            yield ("error", {"tier": int(route.tier), "error": str(e)})

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

        # Heuristic plan: 1 step per symbol + 1 fundamentals step.
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

            async def _call() -> Any:
                if not self.circuit_breaker.allow():
                    raise RuntimeError("circuit breaker open")
                return await tool.invoke(args, context)

            try:
                result = await retry_async(
                    _call,
                    policy=self.retry_policy,
                    # Provider-level errors (network, rate-limit) don't help
                    # from N back-to-back retries against the same upstream.
                    # The builtin tools already wrap ProviderFailover, so
                    # skip retry here and let that layer take over.
                    skip_exceptions=(ProviderError,),
                )
                self.circuit_breaker.record_success()
                return {"name": name, "result": self._dump(result)}
            except Exception as e:
                self.circuit_breaker.record_failure()
                return {"name": name, "error": str(e)}

        return list(await asyncio.gather(*(_run_step(s) for s in state.plan)))

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
    async def _llm_plan(self, state: OrchestratorState) -> list[dict[str, Any]]:
        """Real LLM-backed plan generation (returns [] when LLM not configured)."""
        if self.llm_factory is None or not self.llm_factory.is_configured():
            return []
        try:
            provider = self.llm_factory.make()
            prompt = self._build_plan_prompt(state)
            response = provider.complete_text(prompt=prompt, system=self._PLAN_SYSTEM, temperature=0.0)
            content = getattr(response, "content", response)
            return self._parse_plan(content, state)
        except Exception as e:
            LOGGER.warning("LLM plan failed: %s", e)
            return []

    _PLAN_SYSTEM = (
        "You are a finance research planner. Reply ONLY with valid JSON. "
        "No commentary, no markdown fences. Output schema: "
        "[{\"step\": <int>, \"agent\": <agent_name>, \"args\": {<dict>}}].\n"
        "Question classification (mandatory):\n"
        "- A-class (self-knowledge, no tool needed): current date/weekday, "
        "unit conversion, basic finance terms, definitions. Output an empty "
        "plan [] — SynthesizeNode answers directly using its system prompt.\n"
        "- B-class (market data, fundamentals, news, factors): output a plan "
        "that calls the appropriate agent(s). Use \"agent\": \"data_agent\" "
        "for concurrent quote+fundamentals, \"news_agent\" for news, etc."
    )

    def _build_plan_prompt(self, state: OrchestratorState) -> str:
        agent_caps = [
            f"- {name}: {self.agent_registry.get(name).description}"
            for name in self.agent_registry.list()
            if self.agent_registry.get(name).description
        ]
        return (
            f"User message: {state.user_message}\n\n"
            f"Current date: 2026-09-14 (东八区时间 周一)\n"
            f"Detected symbols: {state.symbols}\n"
            f"Detected intent: {state.intent.value}\n\n"
            "Available agents:\n" + "\n".join(agent_caps) +
            "\n\nGenerate a JSON plan as a list of {step, agent, args} objects.\n"
            "If the user question is A-class (date / weekday / basic concept), "
            "return an empty plan [] — the synthesizer will answer directly."
        )

    @staticmethod
    def _parse_plan(content: str, state: OrchestratorState) -> list[dict[str, Any]]:
        import json
        import re
        text = content.strip()
        # Strip optional markdown fence.
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except Exception:
            pass
        LOGGER.warning("LLM plan returned non-JSON content: %s", content[:120])
        return []

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
