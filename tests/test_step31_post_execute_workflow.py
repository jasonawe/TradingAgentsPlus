"""Step 31 (cont.) — post_execute_workflow builds a valid graph.

The orchestrator wiring isn't exercised here (it requires a full
Harness init), so we mock just the three handler methods and
verify the graph is correctly composed.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _orchestrator_stub():
    """Build a stub orchestrator with the three handlers we need.

    _observe is sync; _verify / _synthesize are async.
    """
    orch = MagicMock()
    # _observe (sync) returns a dict
    orch._observe = MagicMock(return_value={"tool_count": 1, "errors": 0})
    # _verify (async) returns an object with .ok, .level, .details
    class _VR:
        ok = True
        level = "L3"
        details = {"score": 0.9}
    async def _verify(orch_state):
        return _VR()
    orch._verify = _verify
    # _synthesize (async) returns an object — content irrelevant for
    # this test because we use halt=True so emit list is empty.
    async def _synthesize(orch_state):
        return {"final": "ok"}
    orch._synthesize = _synthesize
    return orch


def test_post_execute_workflow_builds_three_nodes():
    from tradingagents.agent_harness.core.post_execute_workflow import (
        build_post_execute_workflow,
    )
    orch = _orchestrator_stub()
    wf = build_post_execute_workflow(orch)
    ids = [n.id for n in wf.nodes()]
    assert ids == ["observe", "verify", "synthesize"]


def test_post_execute_workflow_edges_chain_three_stages():
    from tradingagents.agent_harness.core.post_execute_workflow import (
        build_post_execute_workflow,
    )
    orch = _orchestrator_stub()
    wf = build_post_execute_workflow(orch)
    pairs = [(e.from_node, e.to_node) for e in wf.edges()]
    assert ("observe", "verify") in pairs
    assert ("verify", "synthesize") in pairs
    assert ("synthesize", "<end>") in pairs


def test_post_execute_workflow_runs_end_to_end():
    """Stub handlers run in order; observe/verified events emitted;
    synthesize halts; final state holds orchestrator outputs."""
    from tradingagents.agent_harness.core.post_execute_workflow import (
        build_post_execute_workflow,
    )
    orch = _orchestrator_stub()
    wf = build_post_execute_workflow(orch)

    async def _go():
        events = []
        state = {"orch_state": {"intent": "quote"}}
        async for ev, payload in wf.run(state):
            events.append((ev, payload))
        return events, state
    events, state = asyncio.run(_go())
    names = [e[0] for e in events]
    assert names == ["observed", "verified"]
    # synthesize halts before yielding anything
    assert state["observe_stats"] == {"tool_count": 1, "errors": 0}
    assert state["verify_result"].ok is True
    assert state["final"] == {"final": "ok"}


def test_post_execute_workflow_entry_is_observe():
    from tradingagents.agent_harness.core.post_execute_workflow import (
        build_post_execute_workflow,
    )
    wf = build_post_execute_workflow(_orchestrator_stub())
    assert wf.entry_node().id == "observe"


def test_post_execute_workflow_executed_log():
    """wf.executed() returns the actual node visit order."""
    from tradingagents.agent_harness.core.post_execute_workflow import (
        build_post_execute_workflow,
    )
    wf = build_post_execute_workflow(_orchestrator_stub())

    async def _go():
        async for _ in wf.run({"orch_state": {}}):
            pass
    asyncio.run(_go())
    assert wf.executed() == ["observe", "verify", "synthesize"]
