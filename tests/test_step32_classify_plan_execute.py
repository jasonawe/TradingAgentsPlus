"""Step 32 — classify_plan_execute_workflow builds a valid 5-node graph.

Mirrors test_step31_post_execute_workflow.py for the prefix migration.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _orchestrator_stub():
    """Stub the 5 handler methods the workflow needs."""
    orch = MagicMock()
    orch._plan = AsyncMock(return_value=["plan_step_1", "plan_step_2"])
    orch._execute = AsyncMock(return_value=[
        {"name": "get_quote", "result": {"price": 99.5}},
        {"name": "get_news", "result": {"headline": "..."}},
    ])
    orch._execute_ptc = AsyncMock(return_value=[])
    orch._observe = MagicMock(return_value={"tool_count": 2, "errors": 0})
    class _VR:
        ok = True
        level = 3
    orch._verify = AsyncMock(return_value=_VR())
    orch._synthesize = AsyncMock(return_value={"final": "ok"})
    orch._kick_off_prefetch = MagicMock()
    return orch


def test_workflow_builds_five_nodes():
    from tradingagents.agent_harness.core.classify_plan_execute_workflow import (
        build_classify_plan_execute_workflow,
    )
    wf = build_classify_plan_execute_workflow(_orchestrator_stub())
    ids = [n.id for n in wf.nodes()]
    assert ids == ["plan", "execute", "observe", "verify", "synthesize"]


def test_workflow_edges_chain_five_stages():
    from tradingagents.agent_harness.core.classify_plan_execute_workflow import (
        build_classify_plan_execute_workflow,
    )
    wf = build_classify_plan_execute_workflow(_orchestrator_stub())
    pairs = [(e.from_node, e.to_node) for e in wf.edges()]
    assert ("plan", "execute") in pairs
    assert ("execute", "observe") in pairs
    assert ("observe", "verify") in pairs
    assert ("verify", "synthesize") in pairs
    assert ("synthesize", "<end>") in pairs


def test_workflow_runs_end_to_end():
    """Stub handlers run in order; tool_result + observed + verified events emitted;
    synthesize halts; final state holds orchestrator outputs."""
    from tradingagents.agent_harness.core.classify_plan_execute_workflow import (
        build_classify_plan_execute_workflow,
    )
    orch = _orchestrator_stub()
    wf = build_classify_plan_execute_workflow(orch)

    async def _emit(name, payload):
        return ("_internal", {"name": name, "payload": payload})

    async def _go():
        events = []
        state = {
            "orch_state": {"intent": "quote"},
            "route": MagicMock(),
            "emit": _emit,
        }
        async for ev, payload in wf.run(state):
            events.append((ev, payload))
        return events, state

    events, state = asyncio.run(_go())
    names = [e[0] for e in events if e[0] not in ("_internal",)]
    # All emitted events in order: plan_ready, tool_result x2, observed, verified
    assert "plan_ready" in names
    assert names.count("tool_result") == 2
    assert "observed" in names
    assert "verified" in names
    # synthesize halts — no agent_final event from the workflow
    assert "agent_final" not in names
    # All 5 handlers were called in order
    assert wf.executed() == ["plan", "execute", "observe", "verify", "synthesize"]


def test_workflow_drains_pending_approvals():
    """Pending approvals in orch_state get surfaced as confirm_request events."""
    from tradingagents.agent_harness.core.classify_plan_execute_workflow import (
        build_classify_plan_execute_workflow,
    )
    orch = _orchestrator_stub()
    orch._execute = AsyncMock(return_value=[])
    wf = build_classify_plan_execute_workflow(orch)

    async def _emit(name, payload):
        return ("_internal", {"name": name})

    async def _go():
        events = []
        state = {
            "orch_state": MagicMock(spec=["plan", "tool_results",
                                          "final", "pending_approvals"]),
        }
        state["orch_state"].plan = ["x"]
        state["orch_state"].tool_results = []
        state["orch_state"].final = None
        # Two pending approvals
        state["orch_state"].pending_approvals = [
            {"tool_name": "create_note", "audit_id": 1},
            {"tool_name": "delete_note", "audit_id": 2},
        ]
        state["emit"] = _emit
        async for ev, payload in wf.run(state):
            events.append((ev, payload))
        return events

    events = asyncio.run(_go())
    confirms = [e for e in events if e[0] == "confirm_request"]
    assert len(confirms) == 2


def test_workflow_ptc_mode():
    """When plan is a PTC dict, _execute_ptc is called instead of _execute."""
    from tradingagents.agent_harness.core.classify_plan_execute_workflow import (
        build_classify_plan_execute_workflow,
    )
    orch = _orchestrator_stub()
    orch._plan = AsyncMock(return_value={
        "mode": "ptc", "groups": [{"name": "g1"}],
    })
    orch._execute_ptc = AsyncMock(return_value=[
        {"name": "get_quote", "result": {"price": 100}},
    ])
    wf = build_classify_plan_execute_workflow(orch)

    async def _emit(name, payload):
        return ("_internal", {"name": name})

    async def _go():
        events = []
        state = {
            "orch_state": {"pending_approvals": []},
            "emit": _emit,
        }
        async for ev, payload in wf.run(state):
            events.append((ev, payload))
        return events

    events = asyncio.run(_go())
    # plan_ready_ptc emitted
    assert any(e[0] == "plan_ready_ptc" for e in events)
    # _execute_ptc was called, not _execute
    orch._execute_ptc.assert_awaited_once()
    orch._execute.assert_not_awaited()
