"""Phase 1: RouterPlan → GraphSpec compiler.

Group-order dependency inference (NOT per-call ``depends_on`` because
``ToolCall`` does not have one). Same heuristic as live
``Orchestrator._plan_from_router()``.

NOTE on ``_TOOL_TO_AGENT`` coupling: imported from ``core.orchestrator``
(intentionally — same module that defines the runtime mapping today).
"""
from __future__ import annotations
from typing import Any

from .graph import GraphSpec, Edge, BaseNode
from .nodes import ToolNode

# CRITICAL: this module is 3 levels deep (multi_agent → runtime → agent_harness).
# Three dots up = agent_harness, where core/orchestrator.py lives.
#
# Rev.7 BLOCKER 2 (Aquinas round-6): `_TOOL_TO_AGENT` (defined at line 660 of
# core/orchestrator.py) MUST be imported LAZILY inside `PlanCompiler.compile()`,
# not at module top. Loading `compiler.py` at orchestrator-import time would
# trigger a circular import (orchestrator → multi_agent → compiler → orchestrator).
# orchestrator.py:2947 already documents this class of bug.
_TOOL_TO_AGENT = None  # lazily resolved on first compile() call

class CompileError(Exception):
    """Raised when a RouterPlan cannot be turned into a GraphSpec."""


class PlanCompiler:
    def compile(self, plan: Any) -> GraphSpec:
        calls = list(plan.calls)
        if not calls:
            raise CompileError("empty plan")

        nodes: dict[str, BaseNode] = {}
        edges: list[Edge] = []
        groups: dict[int, list[str]] = {}
        group_order: list[int] = []

        # Rev.7 BLOCKER 2: lazy import of _TOOL_TO_AGENT (avoids circular import).
        global _TOOL_TO_AGENT
        if _TOOL_TO_AGENT is None:
            from ...core.orchestrator import _TOOL_TO_AGENT as _t2a
            _TOOL_TO_AGENT = _t2a

        for idx, call in enumerate(calls):
            node_id = f"call_{idx}"
            agent = _TOOL_TO_AGENT.get(call.tool)
            if agent is None:
                raise CompileError(f"unknown tool: {call.tool}")
            nodes[node_id] = ToolNode(
                id=node_id,
                agent_id=agent,
                tool_name=call.tool,
                raw_args=dict(call.args),
            )
            g = int(getattr(call, "parallel_group", 0) or 0)
            if g not in groups:
                groups[g] = []
                group_order.append(g)
            groups[g].append(node_id)

        for g in group_order:
            join_id = f"join_{g}"
            for nid in groups[g]:
                edges.append(Edge(src=nid, dst=join_id, kind="data"))

        for i in range(1, len(group_order)):
            prev_g = group_order[i - 1]
            cur_g = group_order[i]
            edges.append(Edge(
                src=f"join_{prev_g}",
                dst=groups[cur_g][0],
                kind="data",
            ))

        entry = "call_0" if calls else None
        exit_node = f"join_{group_order[-1]}" if group_order else None
        return GraphSpec(nodes=nodes, edges=edges, entry=entry, exit=exit_node)
