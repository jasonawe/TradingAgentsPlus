"""Stream events tagged with surface — every orchestrator event carries 'surface'.

P0-4: Surface classification.  Verifies that the orchestrator yields
``surface`` in every payload so downstream SSE consumers / audit sinks
can filter cheaply.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock as MM

from tradingagents.agent_harness.core.orchestrator import Orchestrator
from tradingagents.agent_harness.core.surface import Surface
from tradingagents.agent_harness.core.tier import Intent
from tradingagents.agent_harness.tools import ToolContext, ToolRegistry


class _NoOpLLMFactory:
    """Reports configured=True so maybe_degrade_to_tier1 leaves Tier 2
    paths alone; heuristic planner does not invoke LLM."""

    def is_configured(self) -> bool:
        return True

    def make(self, *, mode: str = "deep"):
        raise RuntimeError(
            "_NoOpLLMFactory.make() should not be invoked — heuristic plan path"
        )


def _make_orch(*, llm_factory=None):
    registry = ToolRegistry()
    from tradingagents.agent_harness.tools.base import BaseTool
    from tradingagents.agent_harness.tools.schema import ToolSchema
    from tradingagents.agent_harness.tools.permission import PermissionType

    class _Quote(BaseTool):
        def __init__(self):
            self.schema = ToolSchema(
                name="get_quote", description="x",
                args_schema=dict, result_schema=dict,
                permission=PermissionType.READ,
            )
        @property
        def name(self): return self.schema.name
        async def invoke(self, args, context):
            return {"symbol": (args or {}).get("symbol"), "price": 10.0}

    registry.add(_Quote())
    from tradingagents.agent_harness.core.retry import RetryPolicy, CircuitBreaker
    from tradingagents.agent_harness.core.context import ContextPriority

    return Orchestrator(
        tool_registry=registry,
        agent_registry=MM(list_names=lambda: [], get=lambda n: (_ for _ in ()).throw(KeyError(n))),
        llm_factory=llm_factory,
        context_priority=ContextPriority(),
        retry_policy=RetryPolicy(max_retries=0),
        circuit_breaker=CircuitBreaker(failure_threshold=5, reset_seconds=30.0),
        audit=None,
        enable_l3=False,
    )


def _collect(orch, session_id="t", message="比较 600036 600418"):
    async def _run():
        events = []
        async for ev, p in orch.stream_chat(session_id=session_id, user_message=message):
            events.append((ev, p))
        return events
    return asyncio.run(_run())


# --------------------------------------------------------------------------
# Every event carries a 'surface' field
# --------------------------------------------------------------------------
class TestEveryEventTagged:
    def test_all_events_have_surface_field(self):
        events = _collect(_make_orch())
        assert len(events) > 0
        for ev, payload in events:
            assert "surface" in payload, f"event {ev} missing surface field"
            assert payload["surface"] in {s.value for s in Surface}, \
                f"event {ev} has unknown surface {payload['surface']!r}"

    def test_ui_events_classified_as_ui(self):
        events = _collect(_make_orch(llm_factory=_NoOpLLMFactory()))
        ui_events = [ev for ev, p in events if p["surface"] == "ui"]
        # At minimum, plan_started + plan_ready_ptc + tool_result + agent_final
        assert "plan_started" in ui_events
        assert "tool_result" in ui_events
        assert "agent_final" in ui_events


# --------------------------------------------------------------------------
# Specific event classification
# --------------------------------------------------------------------------
class TestSpecificClassifications:
    def test_usage_summary_tagged_as_debug(self):
        """usage_summary is the canonical DEBUG event (token accounting)."""
        # Synthesize a stream_chat that emits usage_summary by calling
        # stream_chat on an orch with attached store (already wired).
        events = _collect(_make_orch(), message="查 600036")
        ev_names = [ev for ev, _ in events]
        if "usage_summary" in ev_names:
            idx = ev_names.index("usage_summary")
            assert events[idx][1]["surface"] == "debug"

    def test_error_event_tagged_as_ui(self):
        # Force an error path by passing a bad plan
        orch = _make_orch()

        async def _err():
            events = []
            # Patch _plan to return bad shape
            async def _bad_plan(state, context):
                return {"mode": "ptc", "groups": "not a list"}
            orch._plan = _bad_plan
            async for ev, p in orch.stream_chat(session_id="t", user_message="x"):
                events.append((ev, p))
            return events
        events = asyncio.run(_err())
        # The error event should be tagged ui
        errs = [p for ev, p in events if ev == "error"]
        assert len(errs) >= 1
        assert errs[-1]["surface"] == "ui"


# --------------------------------------------------------------------------
# Caller override — explicit surface wins
# --------------------------------------------------------------------------
class TestCallerOverride:
    def test_caller_can_override_surface(self):
        """If a caller emits an event with 'surface' set, the router respects it."""
        from tradingagents.agent_harness.core.surface import SurfaceRouter

        router = SurfaceRouter()
        # Simulate: caller pre-tagged a UI event as INTERNAL
        out = router.tag("plan_started", {"surface": "internal"})
        assert out["surface"] == "internal"
