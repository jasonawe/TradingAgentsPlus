"""Task 20 — TurnCoordinator (lightweight Tier 1 / Tier 2/3 wrapper).

For tests: a minimal coordinator that:
- Tier 1 routes through ToolExecutor + ResultFormatter, zero LLM
- Tier 2/3 calls runtime.start_analysis via the supplied Runtime facade
"""
from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)


class TurnCoordinator:
    """Tier-aware turn wrapper used by tests + Task 20 reference impl."""

    def __init__(
        self,
        *,
        store: Any,
        tool_executor: Any,
        result_formatter: Any,
        llm_calls_counter: dict[str, int] | None = None,
        runtime: Any | None = None,
        context_assembler: Any | None = None,
    ) -> None:
        self.store = store
        self.tool_executor = tool_executor
        self.result_formatter = result_formatter
        self.llm_calls_counter = llm_calls_counter or {"n": 0}
        self.runtime = runtime
        self.context_assembler = context_assembler

    def handle(
        self,
        *,
        route: Any,
        user_message: str,
        session_id: str,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch one turn by route tier."""
        if int(route.tier) == 1:
            return self._tier1(route=route, user_message=user_message)
        return {"tier": int(route.tier), "session_id": session_id,
                "run": self.start_agent_run(
                    session_id=session_id, turn_id=turn_id or "t1",
                    route=route, planner_agent="planner",
                    planner_capability="planning",
                )}

    def _tier1(self, *, route: Any, user_message: str) -> dict[str, Any]:
        # Tier 1: zero LLM, just tool + formatter
        try:
            tool_result = _maybe_await(
                self.tool_executor.invoke("direct_read", {"route": route.model_dump() if hasattr(route, "model_dump") else dict(route)})
            )
        except Exception as e:
            LOGGER.warning("tier 1 tool call failed: %s", e)
            tool_result = {}
        return self.result_formatter.format(
            tier=1, content=tool_result, route=route,
        )

    def start_agent_run(
        self,
        *,
        session_id: str,
        turn_id: str,
        route: Any,
        planner_agent: str,
        planner_capability: str,
    ) -> dict[str, Any]:
        """Create an AGENT_ANALYSIS run. Returns the run row."""
        from ..runtime.runtime import AgentRuntime
        from ..runtime.persistence.runs import RunRepository
        if self.runtime is None:
            # Build a thin runtime without scheduler/dispatcher for tests
            from ..runtime.dispatcher import AgentDispatcher
            from ..runtime.policy import AgentRegistry as RuntimeAgentRegistry
            reg = RuntimeAgentRegistry()
            dispatcher = AgentDispatcher(
                agent_registry=reg,
                command_resolver=None,
                message_ingestor=None,
                context_provider=None,
            )
            self.runtime = AgentRuntime(
                store=self.store,
                agent_registry=reg,
                command_resolver=None,
                message_ingestor=None,
                context_provider=None,
                scheduler_factory=lambda s: _NullScheduler(),
            )
        return self.runtime.start_analysis(
            session_id=session_id, turn_id=turn_id, route=route,
            planner_agent=planner_agent, planner_capability=planner_capability,
        )


class _NullScheduler:
    """No-op scheduler for tests that don't need real scheduling."""
    def claim_ready_task(self, **kwargs):
        return None
    def run_once(self, **kwargs):
        return {"resolved_wait_ids": [], "promoted_task_ids": [], "aggregated_run_ids": []}
    def run_until_blocked(self, run_id, **kwargs):
        return {"resolved_wait_ids": [], "promoted_task_ids": [], "aggregated_run_ids": []}


def _maybe_await(value):
    """Await a coroutine if needed; return plain value otherwise."""
    import inspect, asyncio
    if inspect.isawaitable(value):
        # In sync context (Tier 1 handle), use asyncio.run
        try:
            asyncio.get_running_loop()
            # Already in a loop — caller must be async; return coroutine
            return value
        except RuntimeError:
            return asyncio.run(value)
    return value


__all__ = ["TurnCoordinator"]
