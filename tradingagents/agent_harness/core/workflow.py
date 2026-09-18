"""Step 31 — declarative workflow primitive (D3 spec).

The Orchestrator currently embeds its control flow as imperative
async functions (``_plan`` -> ``_execute`` -> ``_observe`` ->
``_verify`` -> ``_synthesize``). Adding a new node requires editing
the orchestrator and re-running the full code path. D3 (Workflow
independent) moves control flow to a declarative graph that can be:

- loaded from YAML/JSON,
- composed across modules (different workflows per intent),
- walked step-by-step with explicit state passing.

This module provides the *primitive*: ``Node``, ``Edge``, ``Workflow``,
and ``Workflow.run``. Migrating the full orchestrator is incremental
— Step 31 wires the primitive + tests; subsequent steps migrate
specific subgraphs (post-tool observe/verify/synthesize is a good
first candidate because it has 4 well-defined stages).

Why not use an existing library (e.g. langgraph)
-----------------------------------------------
langgraph targets graph-RAG / agent messaging; its state model is
too heavy for our 6-node pipeline. A 100-line primitive gives us
exactly the API surface we need (Node + Edge + async iterator)
without dragging in a dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any, AsyncIterator, Awaitable, Callable,
)
import asyncio


@dataclass
class NodeResult:
    """Return value of a node handler.

    Attributes
    ----------
    state : dict
        Mutable shared state passed between nodes. Each node may
        modify this in-place; the workflow reads it when evaluating
        edges.
    emit : list[tuple[str, dict]]
        Events to surface to the SSE client. Nodes append to this
        list (instead of yielding directly) so the workflow can
        decide ordering / dedup / surface routing.
    next_node : str | None
        Optional explicit next node. When set, edges are bypassed
        (useful for early-return paths like "tier1_short_circuit").
    halt : bool
        When True, the workflow stops after this node. Equivalent
        to ``next_node == "<end>"`` but more readable.
    """
    state: dict[str, Any]
    emit: list[tuple[str, dict]] = field(default_factory=list)
    next_node: str | None = None
    halt: bool = False


# A node handler is an async callable that takes the shared state
# dict and returns a NodeResult.  ``state`` is shared by reference
# so handlers can read prior mutations.
NodeHandler = Callable[[dict[str, Any]], Awaitable[NodeResult]]


@dataclass
class Node:
    """A workflow node = an id + an async handler.

    The id is used by ``Edge.from_node`` / ``Edge.to_node`` and by
    log messages. Handlers MUST be idempotent — a node may be
    re-entered on retry (Step 29 partial replay).
    """
    id: str
    handler: NodeHandler


@dataclass
class Edge:
    """Directed edge in the workflow graph.

    ``condition`` is evaluated against the current state right after
    the source node finishes. When it returns True, the workflow
    advances to ``to_node``. When multiple edges leave a node, the
    first matching condition wins (in declaration order).

    ``priority`` is an integer sort key (lower wins) for nodes with
    multiple outgoing edges. ``0`` is the default; use negative
    numbers to make an edge prefer over a default.
    """
    from_node: str
    to_node: str
    condition: Callable[[dict[str, Any]], bool] = lambda s: True
    priority: int = 0


@dataclass
class FanOut:
    """Parallel execution node — runs multiple child nodes concurrently.

    Use when a workflow stage needs to fan out into independent sub-tasks
    (e.g. fetch quote + fetch news + fetch fundamentals in parallel, then
    converge into a synthesizer).

    State merging
    -------------
    Each child handler receives a SHARED state reference (same as
    sequential nodes), but writes must be coordinated. The convention
    used in this codebase:

    - Each child writes its own slice: ``state["fan_out_id"][child_id]``
    - The framework writes the per-child output under
      ``state["fan_out_results"][child_id]`` for post-hoc inspection.

    Emits
    -----
    Each child's ``emit`` events are surfaced in order of completion,
    prefixed with the fan-out node id so SSE clients can demultiplex.
    Halt semantics: if a child requests ``halt=True``, the fan-out stops
    further children and short-circuits the workflow.

    Edges
    -----
    FanOut participates in edge routing like a regular Node — outgoing
    edges from the FanOut id are evaluated after all children finish.

    Attributes
    ----------
    id : str
        Unique id, used as ``Edge.from_node`` / ``Edge.to_node`` target.
    children : list[Node]
        Sub-nodes to execute concurrently.
    """
    id: str
    children: list[Node] = field(default_factory=list)


END = "<end>"


class Workflow:
    """Declarative async workflow.

    Usage
    -----

    ::

        wf = Workflow()
        wf.add_node(Node("classify", classify_handler))
        wf.add_node(Node("plan", plan_handler))
        wf.add_node(Node("synthesize", synth_handler))
        wf.add_edge(Edge("classify", "plan",
                         condition=lambda s: s.get("intent") != "trivial"))
        wf.add_edge(Edge("plan", "synthesize"))
        wf.add_edge(Edge("synthesize", END))
        wf.set_entry("classify")
        async for event, payload in wf.run({"user_message": "hi"}):
            print(event, payload)

    Adding a new node = ``wf.add_node(Node("validate", validate_handler))``
    + ``wf.add_edge(Edge("plan", "validate"))`` + ``wf.add_edge(Edge("validate", "synthesize"))``.
    No orchestrator edits required.
    """

    def __init__(self, *, name: str = "workflow") -> None:
        self.name = name
        self._nodes: dict[str, Node] = {}
        self._fanouts: dict[str, FanOut] = {}
        self._edges: list[Edge] = []
        self._entry: str = ""
        self._executed: list[str] = []  # execution log for debugging

    # ------------------------------------------------------------------
    # graph mutation
    # ------------------------------------------------------------------
    def add_node(self, node: Node) -> None:
        """Register a node. Raises ValueError on duplicate id."""
        if node.id in self._nodes or node.id in self._fanouts:
            raise ValueError(f"workflow {self.name!r}: duplicate node id {node.id!r}")
        if node.id == END:
            raise ValueError(f"node id {END!r} is reserved")
        self._nodes[node.id] = node

    def add_fanout(self, fanout: FanOut) -> None:
        """Register a fan-out (parallel) node.

        Fan-out id must be unique across regular nodes and other fan-outs.
        """
        if fanout.id in self._nodes or fanout.id in self._fanouts:
            raise ValueError(f"workflow {self.name!r}: duplicate node id {fanout.id!r}")
        if fanout.id == END:
            raise ValueError(f"node id {END!r} is reserved")
        # Children ids must also be globally unique.
        seen_child = set()
        for child in fanout.children:
            if child.id in seen_child or child.id in self._nodes or child.id in self._fanouts:
                raise ValueError(
                    f"workflow {self.name!r}: duplicate child node id {child.id!r}"
                )
            seen_child.add(child.id)
        self._fanouts[fanout.id] = fanout

    def add_edge(self, edge: Edge) -> None:
        """Register a directed edge. from_node must already exist (or
        be added later — we don't enforce ordering)."""
        if edge.to_node != END and edge.to_node not in self._nodes and edge.to_node not in self._fanouts:
            # Allow forward-declared edges; check at run time.
            pass
        self._edges.append(edge)

    def set_entry(self, node_id: str) -> None:
        if node_id not in self._nodes and node_id not in self._fanouts:
            raise ValueError(f"workflow {self.name!r}: unknown entry node {node_id!r}")
        self._entry = node_id

    # ------------------------------------------------------------------
    # introspection (for tests + debugging)
    # ------------------------------------------------------------------
    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def edges(self) -> list[Edge]:
        return list(self._edges)

    def entry_node(self) -> Node | Node | FanOut | None:
        if self._entry in self._nodes:
            return self._nodes[self._entry]
        return self._fanouts.get(self._entry)

    def executed(self) -> list[str]:
        """List of node ids in execution order (read-only copy)."""
        return list(self._executed)

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    async def run(self, state: dict[str, Any]) -> AsyncIterator[tuple[str, dict]]:
        """Walk the workflow starting at the entry node.

        Yields ``(event_name, payload)`` tuples from each node's
        ``emit`` list, in the order nodes execute. Stops on
        ``halt=True`` or when no outgoing edge matches (no-op end).
        """
        if not self._entry:
            raise RuntimeError(f"workflow {self.name!r}: no entry node set")
        current = self._entry
        self._executed = []
        # Safety bound — at most len(nodes) + len(edges) hops before
        # we declare a cycle and bail. Workflows should be DAGs; this
        # bound is a debugging aid, not a runtime guarantee.
        max_hops = max(len(self._nodes) * 4, 32)
        hops = 0
        while current != END and hops < max_hops:
            hops += 1
            # FanOut path
            fanout = self._fanouts.get(current)
            if fanout is not None:
                async for ev, payload in self._run_fanout(fanout, state):
                    yield (ev, payload)
                self._executed.append(fanout.id)
                # Walk outgoing edges (same as Node path)
                outgoing = [e for e in self._edges if e.from_node == fanout.id]
                outgoing.sort(key=lambda e: e.priority)
                matched = None
                for e in outgoing:
                    try:
                        ok = e.condition(state)
                    except Exception:
                        continue
                    if ok:
                        matched = e
                        break
                if matched is None:
                    return
                current = matched.to_node
                continue
            node = self._nodes.get(current)
            if node is None:
                yield ("workflow_error", {
                    "workflow": self.name,
                    "error": f"unknown node {current!r}",
                    "executed": list(self._executed),
                })
                return
            self._executed.append(node.id)
            try:
                result = await node.handler(state)
            except Exception as exc:
                yield ("workflow_error", {
                    "workflow": self.name,
                    "node": node.id,
                    "error": repr(exc),
                    "executed": list(self._executed),
                })
                return
            # Surface emitted events first (SSE ordering).
            for ev, payload in result.emit:
                yield (ev, payload)
            # Sync the (possibly modified) state back to our local
            # reference so the next edge sees the latest values.
            state = result.state
            # Halt short-circuits everything.
            if result.halt:
                return
            # Explicit next_node bypasses edges.
            if result.next_node:
                if result.next_node != END and result.next_node not in self._nodes and result.next_node not in self._fanouts:
                    yield ("workflow_error", {
                        "workflow": self.name,
                        "node": node.id,
                        "error": f"next_node={result.next_node!r} not registered",
                    })
                    return
                current = result.next_node
                continue
            # Walk outgoing edges in priority order; first match wins.
            outgoing = [e for e in self._edges if e.from_node == node.id]
            outgoing.sort(key=lambda e: e.priority)
            matched = None
            for e in outgoing:
                try:
                    ok = e.condition(state)
                except Exception:
                    continue  # misbehaving edge — skip
                if ok:
                    matched = e
                    break
            if matched is None:
                # No outgoing edge matched — implicit end.
                return
            current = matched.to_node
        if hops >= max_hops:
            yield ("workflow_error", {
                "workflow": self.name,
                "error": "max_hops exceeded — suspected cycle",
                "executed": list(self._executed),
            })

    async def _run_fanout(
        self, fanout: FanOut, state: dict[str, Any]
    ) -> AsyncIterator[tuple[str, dict]]:
        """Execute fan-out children concurrently.

        Each child runs against the shared state reference. Emits are
        tagged with the parent fan-out id and child id so consumers can
        demultiplex. Per-child results are stored in
        ``state["fan_out_results"][child_id]``. If any child raises, the
        error is yielded as a ``workflow_error`` event with the
        ``child_id`` field but the other children continue.
        """
        if not fanout.children:
            return

        # Initialize fan-out slice in state so children can write into it.
        if "fan_out_results" not in state:
            state["fan_out_results"] = {}

        async def _run_child(child: Node) -> tuple[str, NodeResult | BaseException]:
            try:
                return (child.id, await child.handler(state))
            except Exception as exc:  # noqa: BLE001 — surface as data
                return (child.id, exc)

        tasks = [asyncio.create_task(_run_child(child)) for child in fanout.children]
        # Track completion order so emits preserve "first done, first out".
        for completed in asyncio.as_completed(tasks):
            child_id, result = await completed
            if isinstance(result, BaseException):
                yield ("workflow_error", {
                    "workflow": self.name,
                    "node": fanout.id,
                    "child_id": child_id,
                    "error": repr(result),
                })
                continue
            state["fan_out_results"][child_id] = {
                "emit": result.emit,
                "halt": result.halt,
            }
            for ev, payload in result.emit:
                yield (ev, {**payload, "_fanout": fanout.id, "_child": child_id})
            if result.halt:
                # Cancel sibling children
                for t in tasks:
                    if not t.done():
                        t.cancel()
                return
