"""Phase 3: RouterPlan → GraphSpec compiler.

Group-order dependency inference (NOT per-call ``depends_on`` because
``ToolCall`` does not have one). Same heuristic as live
``Orchestrator._plan_from_router()``.

Phase 3 (Work unit 2 — spec §4.5 / §4.6 step 2 + step 4):

- Scan every ``call.args`` dict for ``$ref`` strings (``$agent.field``)
  and validate via the Phase 1 ``parse_ref`` resolver.
- Derive cross-call edges from those refs:

  * **Self-ref** (ref_agent == current call's owner agent) → ``CompileError``.
  * **Unknown agent** (ref_agent not in any other call) → ``CompileError``
    (per-agent scoping, spec §4.5).
  * **Downstream group** (ref_agent's group > current's group) → ``CompileError``
    (the referenced agent has not run yet; can't read its output).
  * **Same group** (ref_agent's group == current's group) → no extra
    edge; intra-group sequencing handled by the join node.
  * **Adjacent upstream** (ref_agent's group is the immediately previous
    group in ``group_order``) → emit a ``data`` edge from referenced node
    to current node (forward dependency).
  * **Skip upstream** (ref_agent's group is earlier but NOT immediately
    previous) → emit a ``loop`` edge with ``max_hops=2`` from current
    node back to the referenced node (back-reference / feedback;
    spec §4.6 step 4).

NOTE on ``_TOOL_TO_AGENT`` coupling: imported from ``core.orchestrator``
(intentionally — same module that defines the runtime mapping today).
"""
from __future__ import annotations
from typing import Any

from .graph import GraphSpec, Edge, BaseNode
from .nodes import ToolNode
from .resolver import parse_ref

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
    """Raised when a RouterPlan cannot be turned into a GraphSpec.

    Trigger conditions (Phase 1 + Phase 3):
    - empty plan (Phase 1)
    - unknown tool (Phase 1)
    - $ref to self (Phase 3, spec §4.5)
    - $ref to agent not in plan (Phase 3, per-agent scoping)
    - $ref to downstream group (Phase 3, spec §4.5)
    """


def _extract_refs(args: dict) -> set[tuple[str, str]]:
    """Return set of (agent_id, field_path) tuples referenced via $agent.field.

    Skips values that are not strings, and strings that don't start with
    ``$`` or fail the ``parse_ref`` regex (malformed refs fall back to
    literal at runtime per spec §4.5 graceful degradation).
    """
    refs: set[tuple[str, str]] = set()
    for v in args.values():
        if not isinstance(v, str):
            continue
        if not v.startswith("$"):
            continue
        parsed = parse_ref(v)
        if parsed is None:
            continue
        refs.add(parsed)
    return refs


class PlanCompiler:
    def compile(self, plan: Any) -> GraphSpec:
        calls = list(plan.calls)
        if not calls:
            raise CompileError("empty plan")

        nodes: dict[str, BaseNode] = {}
        edges: list[Edge] = []
        groups: dict[int, list[str]] = {}
        group_order: list[int] = []
        call_node_ids: list[str] = []   # idx → node_id
        call_groups: list[int] = []     # idx → group number
        call_agents: list[str] = []     # idx → agent_id (via _TOOL_TO_AGENT)

        # Rev.7 BLOCKER 2: lazy import of _TOOL_TO_AGENT (avoids circular import).
        global _TOOL_TO_AGENT
        if _TOOL_TO_AGENT is None:
            from ...core.orchestrator import _TOOL_TO_AGENT as _t2a
            _TOOL_TO_AGENT = _t2a

        # ── First pass: map calls → nodes, capture (idx → node_id, group, agent)
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
            call_node_ids.append(node_id)
            call_groups.append(g)
            call_agents.append(agent)

        # ── Second pass: scan args for $refs; emit data/loop edges per
        #    spec §4.5 / §4.6 step 2 / step 4.
        for idx, call in enumerate(calls):
            node_id = call_node_ids[idx]
            current_agent = call_agents[idx]
            current_group = call_groups[idx]

            refs = _extract_refs(dict(call.args))
            if not refs:
                continue

            for ref_agent, _ref_field in refs:
                # Resolve the short name from $ref (e.g., "data") to the
                # full agent_id registered in _TOOL_TO_AGENT
                # (e.g., "data_agent"). Convention: short name is the full
                # id with the trailing "_<role>" suffix stripped
                # ("data_agent" → "data", "trading_agents" → "trading",
                # "synthesizer" → "synthesizer" because no underscore).
                def _matches(full_agent: str, short: str) -> bool:
                    return full_agent == short or full_agent.startswith(short + "_")

                # Find any OTHER call that owns ref_agent (skip current call).
                # A different call from the SAME agent in the same group is
                # a legitimate cross-call ref, NOT a self-ref.
                ref_node_id: str | None = None
                ref_group: int | None = None
                for other_idx, other_agent in enumerate(call_agents):
                    if other_idx == idx:
                        continue
                    if _matches(other_agent, ref_agent):
                        ref_node_id = call_node_ids[other_idx]
                        ref_group = call_groups[other_idx]
                        break

                # No other call owns ref_agent — either self-ref (the
                # current call's own agent matches) or unknown (no call
                # in the plan owns this agent).
                if ref_node_id is None:
                    if _matches(current_agent, ref_agent):
                        raise CompileError(
                            f"call {idx} ({node_id}, agent={current_agent!r}): "
                            f"$ref to self (${ref_agent}.{_ref_field}) is not allowed"
                        )
                    raise CompileError(
                        f"call {idx} ({node_id}, agent={current_agent!r}): "
                        f"$ref to unknown agent ${ref_agent}.{_ref_field} "
                        f"(agent not in plan; per-agent scoping per spec §4.5)"
                    )

                # 4. Same group → no extra edge (group ordering handles it).
                if ref_group == current_group:
                    continue

                # 5. Downstream group → CompileError (agent has not run yet).
                if ref_group is not None and ref_group > current_group:
                    raise CompileError(
                        f"call {idx} ({node_id}, agent={current_agent!r}): "
                        f"$ref to downstream agent ${ref_agent}.{_ref_field} "
                        f"(referenced group {ref_group} > current {current_group}; "
                        f"per-agent scoping per spec §4.5)"
                    )

                # 6. Upstream (ref_group < current_group).
                #    - Adjacent (ref_group is the immediately previous group
                #      in group_order) → data edge (forward dep).
                #    - Skip-ahead (ref_group is earlier but NOT immediately
                #      previous) → loop edge with max_hops=2 (back-reference
                #      / feedback; spec §4.6 step 4).
                ref_idx_in_order = group_order.index(ref_group)
                cur_idx_in_order = group_order.index(current_group)
                is_adjacent = (cur_idx_in_order - ref_idx_in_order == 1)

                if is_adjacent:
                    edges.append(Edge(src=ref_node_id, dst=node_id,
                                      kind="data"))
                else:
                    edges.append(Edge(src=node_id, dst=ref_node_id,
                                      kind="loop", max_hops=2))

        # ── Third pass: original intra-group join edges + inter-group
        #    sequencing (Phase 1 contract).
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
