"""Step 31 — example Workflow migration: post_execute subgraph.

The Orchestrator's post-tool-execution flow is currently spread
across three imperative methods (``_observe`` → ``_verify`` →
``_synthesize``) all called from ``_stream_chat_impl``. This module
demonstrates how that subgraph looks as a declarative Workflow —
the same handlers, same state, same emit ordering, but composed
via nodes/edges instead of an if/elif chain.

Used by ``Orchestrator._post_execute_workflow`` (lazy property).
The orchestrator still owns the handlers (so they keep access to
``self`` + the tool registry); only the *graph* is declarative.

This is intentionally a thin shim — it does NOT replace
``_stream_chat_impl``. Subsequent steps can migrate the
classify→plan→execute prefix in the same style.
"""
from __future__ import annotations

from typing import Any

from .workflow import Edge, Node, NodeResult, Workflow


def build_post_execute_workflow(orchestrator: Any) -> Workflow:
    """Compose the observe → verify → synthesize subgraph declaratively.

    The returned ``Workflow`` reuses the orchestrator's existing
    handler methods (``_observe``, ``_verify``, ``_synthesize``)
    so we don't fork logic. Edges:

        observe  → verify           (always)
        verify   → synthesize       (always — synthesizer always runs)
        synthesize → END

    Why this is useful even though it's a no-op migration
    -----------------------------------------------------
    - Future intent-specific subgraphs (e.g. write-gate flow:
      synthesize → gate → end) can be added as ONE workflow without
      editing orchestrator.py
    - The graph is introspectable (``wf.nodes()`` / ``wf.edges()``)
      for /api/harness/health introspection
    - Cycle / missing-node detection moves from runtime crashes to
      workflow_error events
    """
    wf = Workflow(name="post-execute")

    async def _observe_node(state: dict[str, Any]) -> NodeResult:
        """Adapter for ``Orchestrator._observe`` (currently sync).

        The orchestrator's _observe returns a dict of stats; we wrap
        the mutation so the workflow can keep going.
        """
        stats = orchestrator._observe(state["orch_state"])
        state["observe_stats"] = stats
        return NodeResult(state, emit=[("observed", stats)])

    async def _verify_node(state: dict[str, Any]) -> NodeResult:
        """Adapter for ``Orchestrator._verify`` (async)."""
        result = await orchestrator._verify(state["orch_state"])
        state["verify_result"] = result
        # Re-emit as ``verified`` so SSE clients see it.
        emit = [("verified", {
            "ok": result.ok,
            "level": result.level.value if hasattr(result.level, "value") else str(result.level),
            "details": result.details or {},
        })]
        return NodeResult(state, emit=emit)

    async def _synthesize_node(state: dict[str, Any]) -> NodeResult:
        """Adapter for ``Orchestrator._synthesize`` (async)."""
        final = await orchestrator._synthesize(state["orch_state"])
        state["final"] = final
        # Synthesizer pushes agent_final internally via _friendly_summary;
        # we just halt so the orchestrator stops walking the workflow.
        return NodeResult(state, emit=[], halt=True)

    wf.add_node(Node("observe", _observe_node))
    wf.add_node(Node("verify", _verify_node))
    wf.add_node(Node("synthesize", _synthesize_node))
    wf.add_edge(Edge("observe", "verify"))
    wf.add_edge(Edge("verify", "synthesize"))
    wf.add_edge(Edge("synthesize", "<end>"))
    wf.set_entry("observe")
    return wf
