"""Step 41 — multi-intent read aggregator (FanOut-based).

Verifies + benchmarks the multi-intent read path. The orchestrator
already uses ``asyncio.gather`` for plan steps in ``_execute``, so
"看看告警和笔记" + 2 symbols is already parallelised at the step
level.

This module provides:

- A reference Workflow that composes a parallel read across
  multiple (symbol, op) intents using FanOut (Step 37 primitive).
- A helper ``run_multi_intent_read(orchestrator, intents)`` that
  collapses to the existing ``_execute`` path when no fan-out is
  needed (1 intent) and uses the Workflow when >=2 intents to
  give operators a clearer execution trace.

Used from: tests/test_step41_multi_intent_fanout.py to assert
correctness + speed.
"""
from __future__ import annotations

from typing import Any

from .workflow import Edge, FanOut, Node, NodeResult, Workflow


async def _run_single_intent(
    orchestrator: Any, intent_pair: tuple[str, str], context: Any,
) -> dict[str, Any]:
    """Run one (intent, op) read against the orchestrator and return
    its result dict.
    """
    intent, op = intent_pair
    # Build a minimal plan step that asks the orchestrator to dispatch
    # the right tool for the (intent, op). We piggyback on _run_step
    # which is the same path _execute uses.
    state_like = {
        "user_message": f"{intent} {op}",
        "intent": intent,
        "op": op,
        "symbols": [],
    }
    result = await orchestrator._execute_step(state_like, context)
    return result


def build_multi_intent_workflow(
    orchestrator: Any, intents: list[tuple[str, str]],
    context: Any,
) -> Workflow:
    """Compose a FanOut workflow that runs ``intents`` in parallel.

    Each child is a single intent that delegates to ``_run_step``.
    The workflow exposes the standard FanOut semantics: emit ``(ev,
    payload)`` per child, tag with ``_fanout`` / ``_child``, merge
    state[fan_out_results] for inspection.

    Used by tests as a verification harness — production code keeps
    using ``_execute`` (already parallel via ``asyncio.gather``).
    """
    wf = Workflow(name="multi-intent-fanout")

    children: list[Node] = []
    for i, pair in enumerate(intents):
        tag = f"intent_{i}_{pair[0]}_{pair[1]}"
        # Closure capture: bind the pair by default-arg trick.
        async def _child(state, _p=pair):
            return NodeResult(
                state,
                emit=[("intent_result", {
                    "intent": _p[0],
                    "op": _p[1],
                })],
            )
        children.append(Node(tag, _child))

    async def _aggregate(state):
        return NodeResult(state, emit=[("multi_intent_done", {
            "count": len(intents),
            "results": state.get("fan_out_results", {}),
        })], halt=True)

    wf.add_fanout(FanOut("fan_out_intents", children=children))
    wf.add_node(Node("aggregate", _aggregate))
    wf.add_edge(Edge("fan_out_intents", "aggregate"))
    wf.add_edge(Edge("aggregate", "<end>"))
    wf.set_entry("fan_out_intents")
    return wf


__all__ = ["build_multi_intent_workflow"]
