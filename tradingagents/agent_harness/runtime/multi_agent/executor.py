"""Phase 2 GraphExecutor.

Walks a GraphSpec using a priority queue. Honours budget + hop guards.

Contracts (Phase 2 Work unit 5 — spec §4.7 strict):
- Per-edge loop accounting lives on ``state.edge_hops[(src, dst)]``,
  NOT on ``Edge`` instances (``Edge.hops_used`` removed).
- ``run()`` resets ``state.edge_hops = {}`` at the top of every
  invocation so the same executor+spec pair is reusable across turns.
- Every edge fire calls ``state.append_inbox(new_msg)`` so downstream
  ``consume_inbox(receiver)`` sees upstream outputs (spec §4.7).
- Global ``state.hops_remaining`` decrements ONCE per while-body iteration
  (NOT per-edge).
- Heartbeat logs use ``initial_hops - state.hops_remaining``.
- ``_enqueue`` parameter ``dest_node`` is the DESTINATION node (its kind
  determines heapq priority), not the source.
- ``self._seq`` resets to 0 at top of ``run()`` for clean per-run isolation.

Per-edge loop counter is read via ``state.edge_hops.get((edge.src, edge.dst), 0)``
in ``_edge_fires`` and incremented to ``state.edge_hops[(src, dst)] += 1``
after each loop fire.
"""
from __future__ import annotations
import heapq
import logging
from dataclasses import dataclass, field
from typing import Optional

from .graph import GraphSpec, Edge, BaseNode
from .nodes import ToolNode, LLMNode, SubplanNode, ConsultNode
from .state import GraphState, Message, TypedResult

LOGGER = logging.getLogger(__name__)

# Priority by node kind (lower = earlier)
_PRIORITY = {
    "tool": 2,
    "llm": 1,
    "subplan": 4,
    "consult": 3,
}


@dataclass(order=True)
class _Pending:
    priority: int
    seq: int
    msg: Message = field(compare=False)


class GraphExecutor:
    def __init__(self, *, max_hops: int = 8, llm_budget: int = 5,
                 consultation_rate_limit: float = 0.5,
                 consultation_max_depth: int = 3) -> None:
        self.max_hops = max_hops
        self.llm_budget = llm_budget
        self.consultation_rate_limit = consultation_rate_limit
        # §0.4.35 phase 2 (Work unit 4) — refuses ConsultNode invocations
        # past this depth so a misbehaving graph can't recurse forever.
        self.consultation_max_depth = consultation_max_depth
        self._seq = 0

    async def run(self, spec: GraphSpec, state: GraphState) -> GraphState:
        # Phase 2 Work unit 5 (spec §4.7): per-run reset of per-edge
        # loop counters. Lives on state (NOT on Edge), so rebind to
        # a fresh dict rather than mutate — guarantees no carry-over
        # across turns even if a caller mutated state.edge_hops between
        # invocations.
        state.edge_hops = {}
        self._seq = 0  # rev.5 LOW #7: clean per-run isolation

        initial_hops = state.hops_remaining
        LOGGER.info(
            "multi_agent: GraphExecutor starting (spec=%d nodes, %d edges, "
            "initial_hops=%d, budget=%d)",
            len(spec.nodes), len(spec.edges),
            initial_hops, state.budget_limit,
        )

        state.hops_remaining = min(state.hops_remaining, self.max_hops)
        state.budget_limit = min(state.budget_limit, self.llm_budget)

        entry = spec.entry or (next(iter(spec.nodes)) if spec.nodes else None)
        if entry is None:
            LOGGER.info("multi_agent: GraphExecutor finished (empty graph)")
            return state

        # Bootstrap: enqueue entry activation. Note that the bootstrap msg
        # is NOT deposited into state.inbox[entry] — entry nodes must use
        # self.raw_args for Phase 1 input. Phase 3 will wire $ref resolution
        # and state.agent_outputs population (see state.py docstring).
        queue: list[_Pending] = []
        self._enqueue(
            queue, entry,
            Message(sender="__start__", receiver=entry,
                    payload=TypedResult(schema=dict, data={}, meta={})),
            dest_node=spec.node(entry),
        )

        activated: set[str] = set()
        while queue and state.hops_remaining > 0 and state.llm_used < state.budget_limit:
            item = heapq.heappop(queue)
            msg = item.msg
            if msg.receiver not in spec.nodes:
                continue
            has_loop = any(
                e.src == msg.receiver and e.kind == "loop" for e in spec.edges
            )
            if msg.receiver in activated and not has_loop:
                continue
            activated.add(msg.receiver)

            node = spec.node(msg.receiver)
            inbox = state.consume_inbox(msg.receiver)

            try:
                out_messages = await self._invoke(node, state, inbox)
            except NotImplementedError as exc:
                LOGGER.warning("node %s not implemented in phase 1: %s", node.id, exc)
                continue
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("node %s failed: %s", node.id, exc)
                continue

            for m in out_messages:
                for edge in spec.edges_from(node.id):
                    if not self._edge_fires(edge, state, m):
                        continue
                    new_msg = Message(
                        sender=node.id, receiver=edge.dst,
                        payload=m.payload, kind=m.kind, hop=m.hop + 1,
                    )
                    state.append_inbox(new_msg)
                    self._enqueue(
                        queue, edge.dst, new_msg,
                        dest_node=spec.node(edge.dst),
                    )
                    if edge.kind == "loop":
                        # Phase 2 Work unit 5: per-edge counter on state.
                        key = (edge.src, edge.dst)
                        state.edge_hops[key] = state.edge_hops.get(key, 0) + 1
                        loop_msg = Message(
                            sender=node.id, receiver=edge.src,
                            payload=m.payload, kind=m.kind, hop=m.hop + 1,
                        )
                        state.append_inbox(loop_msg)
                        self._enqueue(
                            queue, edge.src, loop_msg,
                            dest_node=spec.node(edge.src),
                        )
            # rev.4: global counter decrements ONCE per iteration
            state.hops_remaining -= 1

        LOGGER.info(
            "multi_agent: GraphExecutor finished (hops_used=%d, msgs=%d)",
            initial_hops - state.hops_remaining,
            len(state.message_log),
        )
        return state

    async def _invoke(self, node, state: GraphState,
                      inbox: list[Message]) -> list[Message]:
        """Dispatch to the node's ``run()`` and ALWAYS record the outcome
        in ``state.agent_outputs[node.id]`` — Phase 3 Work unit 1 contract.

        Happy path: writes the last message's ``TypedResult`` payload as the
        node's canonical output.

        Failure path (any exception from ``node.run()``): writes a
        ``TypedResult(data=None, meta={ok=False, error=...})`` placeholder
        so downstream ``$ref`` resolution (Phase 3 Work unit 3) can detect
        the failure.

        For ConsultNode: ``last-write-wins`` semantics — Phase 2's internal
        write inside ``ConsultNode.run`` is overwritten by the executor's
        write here, but the ``data`` shape (``{"ok": True, ...}``) is
        equivalent so downstream ``$ref`` resolves to the same answer.
        """
        try:
            out_messages = await self._dispatch_to_node(node, state, inbox)
        except Exception as exc:
            # Phase 3 Work unit 1: failure marker so $ref can detect.
            state.agent_outputs[node.id] = TypedResult(
                schema=dict,
                data=None,
                meta={
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "source_agent": getattr(node, "agent_id", None),
                },
            )
            raise

        if out_messages:
            state.agent_outputs[node.id] = out_messages[-1].payload
        return out_messages

    async def _dispatch_to_node(self, node, state: GraphState,
                                inbox: list[Message]) -> list[Message]:
        # Phase 2: budget accounting is owned by the node implementations
        # (LLMNode.run increments llm_used AFTER the LLM call; consult_subagent
        # increments consultation_used AFTER the guards fire). The executor
        # simply dispatches and observes — no double-counting.
        #
        # Phase 1 used to increment here so stubbed nodes that raised
        # NotImplementedError didn't bump the counter. Phase 2 nodes never
        # raise on the happy path (LLMNode raises ONLY when no provider is
        # wired, which the executor's outer try/except NotImplementedError
        # catches — same observable behaviour).
        if isinstance(node, ToolNode):
            return await node.run(state, inbox)
        if isinstance(node, LLMNode):
            return await node.run(state, inbox)
        if isinstance(node, ConsultNode):
            # §0.4.35 phase 2 (Work unit 4) — depth guard.
            # If we're already at the configured max depth, refuse the
            # consult: emit a single ``ok=False`` message addressed to
            # the consult node itself, do NOT delegate to ConsultNode.run,
            # and do NOT bump ``state.consultation_used`` (the inner
            # consult_subagent owns that counter).
            if state.consultation_depth >= self.consultation_max_depth:
                refused = TypedResult(
                    schema=dict,
                    data={"ok": False, "error": "ConsultDepthExceeded"},
                    meta={
                        "refused": True,
                        "consultation_depth": state.consultation_depth,
                        "max_depth": self.consultation_max_depth,
                    },
                )
                refused_msg = Message(
                    sender=node.id, receiver=node.id,
                    payload=refused, kind="answer", hop=0,
                )
                # Land the refusal in state.message_log so observers
                # (tests, observability) can see it even when the spec
                # has no downstream edges from this node.
                state.append_inbox(refused_msg)
                return [refused_msg]
            state.consultation_depth += 1
            try:
                return await node.run(state, inbox)
            finally:
                # Decrement even on ConsultNode.run raising — depth must
                # return to its pre-invocation value so the next consult
                # in the same graph isn't refused due to a prior failure.
                state.consultation_depth -= 1
        if isinstance(node, SubplanNode):
            return await node.run(state, inbox)
        raise NotImplementedError(f"unknown node kind: {type(node).__name__}")

    def _edge_fires(self, edge: Edge, state: GraphState, msg: Message) -> bool:
        if edge.kind == "data":
            return True
        if edge.kind == "when":
            if edge.predicate is None:
                return False
            try:
                return bool(edge.predicate(state, msg))
            except Exception:
                return False
        if edge.kind == "loop":
            # Phase 2 Work unit 5: counter lives on state, NOT Edge.
            # Missing key (edge never fired) treated as 0.
            used = state.edge_hops.get((edge.src, edge.dst), 0)
            return used < edge.max_hops
        return False

    def _enqueue(self, queue: list[_Pending], receiver: str,
                 msg: Message, *, dest_node: Optional[BaseNode] = None) -> None:
        # rev.5 LOW #6: parameter is the DESTINATION node (its kind drives
        # priority). Previously named `source_node` which was misleading.
        if dest_node is not None:
            prio = _PRIORITY.get(dest_node.kind.value, 5)
        else:
            prio = 5
        self._seq += 1
        heapq.heappush(queue, _Pending(priority=prio, seq=self._seq, msg=msg))
