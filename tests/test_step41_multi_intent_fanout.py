"""Step 41 — multi-intent read aggregator.

The orchestrator already parallelises plan steps via asyncio.gather
in _execute. This test covers:

- Multi-intent workflow factory builds the expected node graph
- FanOut emits per-child events with _fanout / _child tags
- 3 parallel reads finish in ~1x latency, not 3x
- Backward compat: 1 intent still produces a valid workflow
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class _StubOrchestrator:
    """Mimics orchestrator._execute_step with controllable latency."""

    def __init__(self, latency: float = 0.05):
        self.latency = latency
        self.calls: list[tuple[str, str]] = []

    async def _execute_step(self, state, context):
        self.calls.append((state.get("intent"), state.get("op")))
        await asyncio.sleep(self.latency)
        return {
            "name": f"{state.get('intent')}_{state.get('op')}",
            "ok": True,
            "result": {"echo": True},
        }


def test_multi_intent_workflow_runs_in_parallel():
    """Three intents should finish in ~latency, not ~3*latency."""
    from tradingagents.agent_harness.core.multi_intent_fanout import (
        build_multi_intent_workflow,
    )

    async def main():
        orch = _StubOrchestrator(latency=0.05)
        intents = [
            ("notes", "list"),
            ("alerts", "list"),
            ("watchlist", "list"),
        ]
        wf = build_multi_intent_workflow(orch, intents, context=None)
        events = []
        t0 = time.monotonic()
        async for ev, p in wf.run({}):
            events.append((ev, p))
        elapsed = time.monotonic() - t0
        # 3 parallel children @ 0.05s = ~0.05s total, vs 0.15s serial.
        assert elapsed < 0.12, f"fan-out didn't run in parallel: {elapsed:.2f}s"
        # All 3 children emitted.
        tags = [p.get("_child") for ev, p in events
                if ev == "intent_result"]
        assert len(tags) == 3

    asyncio.run(main())


def test_single_intent_workflow_still_valid():
    """A 1-intent workflow still produces a valid (parallel) DAG."""
    from tradingagents.agent_harness.core.multi_intent_fanout import (
        build_multi_intent_workflow,
    )

    async def main():
        orch = _StubOrchestrator(latency=0.01)
        wf = build_multi_intent_workflow(orch, [("notes", "list")], context=None)
        events = []
        async for ev, p in wf.run({}):
            events.append((ev, p))
        kinds = [ev for ev, _ in events]
        assert "intent_result" in kinds
        assert "multi_intent_done" in kinds

    asyncio.run(main())


def test_fan_out_state_records_per_child_results():
    """fan_out_results dict aggregates each child NodeResult."""
    from tradingagents.agent_harness.core.multi_intent_fanout import (
        build_multi_intent_workflow,
    )

    async def main():
        orch = _StubOrchestrator(latency=0.01)
        wf = build_multi_intent_workflow(orch, [
            ("notes", "list"),
            ("alerts", "list"),
        ], context=None)
        state = {}
        async for _, _ in wf.run(state):
            pass
        assert "fan_out_results" in state
        results = state["fan_out_results"]
        assert len(results) == 2
        # Each child has its emit list captured.
        for child_id, payload in results.items():
            assert "emit" in payload
            assert payload["emit"][0][0] == "intent_result"

    asyncio.run(main())


def test_multi_intent_workflow_viz_serializable():
    """The workflow can be JSON-serialised for /api/harness/workflows."""
    from tradingagents.agent_harness.core.multi_intent_fanout import (
        build_multi_intent_workflow,
    )
    from tradingagents.agent_harness.core.workflow_viz import to_json

    orch = _StubOrchestrator()
    wf = build_multi_intent_workflow(orch, [
        ("notes", "list"),
        ("alerts", "list"),
    ], context=None)
    j = to_json(wf)
    assert j["name"] == "multi-intent-fanout"
    assert any(n["kind"] == "fanout" for n in j["nodes"])
    parallel = [e for e in j["edges"] if e["kind"] == "parallel"]
    assert len(parallel) == 2


def test_intents_preserve_order_in_workflow_children():
    """The order of intents is preserved (children list order)."""
    from tradingagents.agent_harness.core.multi_intent_fanout import (
        build_multi_intent_workflow,
    )

    orch = _StubOrchestrator()
    intents = [
        ("notes", "list"),
        ("alerts", "list"),
        ("watchlist", "list"),
        ("reports", "list"),
    ]
    wf = build_multi_intent_workflow(orch, intents, context=None)
    fanout = wf._fanouts["fan_out_intents"]
    child_ids = [c.id for c in fanout.children]
    # Each child id should mention its intent+op pair.
    assert "intent_0_notes_list" in child_ids[0]
    assert "intent_3_reports_list" in child_ids[-1]
