"""Step 32 — example Workflow migration: classify -> plan -> execute prefix.

The Orchestrator's Tier 2/3 prefix is currently 5 imperative blocks
inside ``_stream_chat_impl``: plan_started -> _plan -> plan_ready ->
_execute -> tool_result -> _observe -> _verify -> _synthesize ->
agent_final. This module mirrors that flow as a declarative Workflow.

Like ``post_execute_workflow`` (Step 31b), the orchestrator still
owns the handler methods. Only the *graph* is declarative.

Why this matters even though _stream_chat_impl is unchanged
----------------------------------------------------------
- Future workflow variants (e.g. "fast path" that skips observe
  for trivial queries, "deep path" with retry loop) can be added
  as separate Workflow instances without forking the orchestrator.
- The graph is introspectable (``wf.nodes()`` / ``wf.edges()``)
  for /api/harness/health + future UI visualization.
- The `executed()` log shows the actual node order in the workflow,
  so debug surfaces can render a flow trace.

Usage
-----
::

    orch = ...
    wf = build_classify_plan_execute_workflow(orch)
    state = {"orch_state": orch_state, "route": route, "emit": emit_fn}
    async for ev, payload in wf.run(state):
        ...
"""
from __future__ import annotations

from typing import Any

from .workflow import Edge, Node, NodeResult, Workflow


def build_classify_plan_execute_workflow(orchestrator: Any) -> Workflow:
    """Compose the classify → plan → execute → observe → verify →
    synthesize prefix declaratively.

    The 6 nodes correspond to the 6 emits in
    ``Orchestrator._stream_chat_impl``. Edges are unconditional;
    ``synthesize`` halts (the actual ``agent_final`` emission
    happens inside ``_synthesize`` via ``_friendly_summary``).
    """
    wf = Workflow(name="classify-plan-execute")

    async def _plan_node(state: dict[str, Any]) -> NodeResult:
        orch_state = state["orch_state"]
        # route / context are optional — _plan reads them off
        # orch_state.symbols / orch_state.user_message anyway. Tests
        # that don't care about the route can pass state={}.
        emit = state["emit"]
        plan = await orchestrator._plan(orch_state, state.get("context"))
        if isinstance(orch_state, dict):
            orch_state["plan"] = plan
        else:
            orch_state.plan = plan
        # §7.3 #9: schedule parallel tool calls so the execute node
        # can drain them.
        orchestrator._kick_off_prefetch(orch_state, state.get("context"))
        is_ptc = isinstance(plan, dict) and plan.get("mode") == "ptc"
        ev_name = "plan_ready_ptc" if is_ptc else "plan_ready"
        ev_payload = (
            {"groups": plan.get("groups", [])}
            if is_ptc
            else {"steps": plan}
        )
        emit_result = await emit(ev_name, ev_payload)
        # Forward plan_ready as an emit event from the workflow.
        return NodeResult(state, emit=[(ev_name, ev_payload), ("_internal", emit_result)])

    async def _execute_node(state: dict[str, Any]) -> NodeResult:
        orch_state = state["orch_state"]
        emit = state["emit"]
        plan = (
            orch_state.get("plan") if isinstance(orch_state, dict)
            else orch_state.plan
        )
        is_ptc = isinstance(plan, dict) and plan.get("mode") == "ptc"
        if is_ptc:
            results = await orchestrator._execute_ptc(
                orch_state, state.get("context"),
            )
        else:
            results = await orchestrator._execute(
                orch_state, state.get("context"),
            )
        if isinstance(orch_state, dict):
            orch_state["tool_results"] = results
        else:
            orch_state.tool_results = results
        emit_list = [("tool_result", r) for r in results]
        # §P3-3+ HITL — drain pending approvals into confirm_request
        pending = (
            list(orch_state.get("pending_approvals", []) or [])
            if isinstance(orch_state, dict)
            else list(getattr(orch_state, "pending_approvals", []) or [])
        )
        for gate in pending:
            emit_list.append(("confirm_request", gate))
        if isinstance(orch_state, dict):
            orch_state["pending_approvals"] = []
        else:
            orch_state.pending_approvals = []
        return NodeResult(state, emit=emit_list)

    async def _observe_node(state: dict[str, Any]) -> NodeResult:
        orch_state = state["orch_state"]
        observations = orchestrator._observe(orch_state)
        return NodeResult(state, emit=[("observed", observations)])

    async def _verify_node(state: dict[str, Any]) -> NodeResult:
        orch_state = state["orch_state"]
        verification = await orchestrator._verify(orch_state)
        return NodeResult(state, emit=[("verified", {
            "ok": verification.ok,
            "level": int(verification.level),
        })])

    async def _synthesize_node(state: dict[str, Any]) -> NodeResult:
        orch_state = state["orch_state"]
        final = await orchestrator._synthesize(orch_state)
        if isinstance(orch_state, dict):
            orch_state["final"] = final
        else:
            orch_state.final = final
        # Synthesizer pushes agent_final internally via _friendly_summary;
        # we just halt so the workflow stops here.
        return NodeResult(state, emit=[], halt=True)

    wf.add_node(Node("plan", _plan_node))
    wf.add_node(Node("execute", _execute_node))
    wf.add_node(Node("observe", _observe_node))
    wf.add_node(Node("verify", _verify_node))
    wf.add_node(Node("synthesize", _synthesize_node))
    wf.add_edge(Edge("plan", "execute"))
    wf.add_edge(Edge("execute", "observe"))
    wf.add_edge(Edge("observe", "verify"))
    wf.add_edge(Edge("verify", "synthesize"))
    wf.add_edge(Edge("synthesize", "<end>"))
    wf.set_entry("plan")
    return wf
