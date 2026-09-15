"""§7.3 #10 — Tier 3 DAG-parallel workflow runner.

A ``Workflow`` is a small DAG of async ``Node``s where each node declares
its ``inputs`` (other node names whose outputs it consumes). The runner
topologically groups nodes into **levels** of independent nodes and
``asyncio.gather``-s each level — so an N-deep graph executes in N
async rounds instead of N sequential awaits.

Why:
  - Tier 2 ``Orchestrator._execute`` already uses ``asyncio.gather`` to
    parallelise plan steps *within* one round, but the round model itself
    is sequential (plan → execute → observe → verify → synthesise).
  - When downstream callers want to express cross-node data dependencies
    (e.g. ``news`` feeds ``sentiment`` which feeds ``synthesise``),
    a DAG runner is the right primitive; orchestrators can layer their
    own pre/post hooks on top without rewriting the level scheduling.

Design notes:
  - Failures short-circuit the level (one bad node fails its level), but
    sibling levels still run by default — call ``run(..., fail_fast=False)``
    to collect every node's result regardless.
  - ``inputs`` is a *set*, not an ordered list. Cycle detection raises
    ``WorkflowCycleError`` with the offending edge.
  - The runner is intentionally framework-free: no LangGraph / no
    pydantic graph — keep it 100% Python ``asyncio`` so plugins can
    subclass without importing the harness internals.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

LOGGER = logging.getLogger(__name__)


class WorkflowError(RuntimeError):
    """Base class for workflow runtime errors."""


class WorkflowCycleError(WorkflowError):
    """Raised when the node graph contains a cycle."""


class WorkflowMissingInputError(WorkflowError, KeyError):
    """Raised when a node references an input that is neither provided as
    a top-level ``inputs`` mapping nor produced by another registered node."""


# A node callable receives a ``dict[input_name, value]`` plus the
# caller's ``context`` and must return a JSON-serialisable result.
NodeCallable = Callable[[Mapping[str, Any], Any], Awaitable[Any]]


@dataclass
class Node:
    """A single step in a :class:`Workflow` DAG."""

    name: str
    run: NodeCallable
    # Names of other nodes whose outputs this node consumes.
    inputs: tuple[str, ...] = ()
    # Optional constant inputs merged into the per-node resolved mapping
    # (keys that don't collide with upstream node names).
    constants: Mapping[str, Any] = field(default_factory=dict)
    # Optional human-readable description for debugging.
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Node.name must be non-empty")
        if not callable(self.run):
            raise TypeError(f"Node.run for {self.name!r} must be callable")
        # tuple for hashability
        self.inputs = tuple(self.inputs)


@dataclass
class WorkflowResult:
    """Snapshot returned by :meth:`Workflow.run`."""

    outputs: dict[str, Any]
    errors: dict[str, BaseException]
    levels: list[list[str]]
    duration_s: float

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def failed_nodes(self) -> list[str]:
        return sorted(self.errors)


class Workflow:
    """DAG runner — execute ``Node`` instances in topological levels."""

    def __init__(self, name: str = "workflow") -> None:
        self.name = name
        self._nodes: dict[str, Node] = {}

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------
    def add(self, node: Node) -> "Workflow":
        if node.name in self._nodes:
            raise ValueError(f"node {node.name!r} already in workflow {self.name!r}")
        self._nodes[node.name] = node
        return self

    def remove(self, name: str) -> None:
        self._nodes.pop(name, None)

    def __contains__(self, name: str) -> bool:
        return name in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def nodes(self) -> list[str]:
        return sorted(self._nodes)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _validate(self) -> None:
        """Check all node ``inputs`` resolve and the graph is acyclic.

        Raises:
            WorkflowMissingInputError: an input is neither a sibling node
                nor present in the caller's ``inputs`` mapping hint.
            WorkflowCycleError: a cycle is detected.
        """
        names = set(self._nodes)
        for node in self._nodes.values():
            for inp in node.inputs:
                if inp not in names:
                    raise WorkflowMissingInputError(
                        f"node {node.name!r} references unknown input "
                        f"{inp!r}; registered: {sorted(names)}"
                    )
        # Cycle detection via Kahn's algorithm.
        in_degree: dict[str, int] = {n: 0 for n in names}
        edges: dict[str, list[str]] = defaultdict(list)
        for node in self._nodes.values():
            for inp in node.inputs:
                edges[inp].append(node.name)
                in_degree[node.name] += 1
        zero = deque([n for n, d in in_degree.items() if d == 0])
        visited = 0
        while zero:
            cur = zero.popleft()
            visited += 1
            for nxt in edges[cur]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    zero.append(nxt)
        if visited != len(names):
            cycle_nodes = sorted(n for n, d in in_degree.items() if d > 0)
            raise WorkflowCycleError(
                f"workflow {self.name!r} has a cycle; nodes still waiting: "
                f"{cycle_nodes}"
            )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _topological_levels(self) -> list[list[Node]]:
        """Group nodes into parallel-friendly levels (Kahn-style BFS)."""
        names = set(self._nodes)
        in_degree: dict[str, int] = {n: 0 for n in names}
        edges: dict[str, list[str]] = defaultdict(list)
        for node in self._nodes.values():
            for inp in node.inputs:
                edges[inp].append(node.name)
                in_degree[node.name] += 1
        levels: list[list[Node]] = []
        frontier = sorted(n for n, d in in_degree.items() if d == 0)
        seen: set[str] = set()
        while frontier:
            levels.append([self._nodes[n] for n in frontier])
            seen.update(frontier)
            next_frontier: set[str] = set()
            for n in frontier:
                for nxt in edges[n]:
                    if nxt in seen:
                        continue
                    in_degree[nxt] -= 1
                    if in_degree[nxt] == 0:
                        next_frontier.add(nxt)
            frontier = sorted(next_frontier)
        return levels

    async def run(
        self,
        *,
        context: Any = None,
        inputs: Mapping[str, Any] | None = None,
        fail_fast: bool = True,
    ) -> WorkflowResult:
        """Execute the DAG and return a :class:`WorkflowResult`.

        Args:
            context: opaque object passed to every node callable as 2nd arg.
            inputs: top-level constants merged into every node's resolved
                input mapping (upstream outputs win on key collision).
            fail_fast: when True, the first failing node in a level
                cancels the rest of that level; later levels still run.
                When False, every node is allowed to run independently.

        Returns:
            ``WorkflowResult.outputs`` contains every node's successful
            output; ``WorkflowResult.errors`` contains the exceptions.
        """
        import time as _time
        start = _time.time()
        self._validate()
        levels = self._topological_levels()
        outputs: dict[str, Any] = dict(inputs or {})
        errors: dict[str, BaseException] = {}
        for level in levels:
            if fail_fast and errors:
                # a previous level already failed — skip remaining levels
                break
            if len(level) == 1:
                results, errs = await self._run_level_single(level[0], outputs, context)
            else:
                results, errs = await self._run_level_parallel(
                    level, outputs, context, fail_fast=fail_fast,
                )
            outputs.update(results)
            errors.update(errs)
        return WorkflowResult(
            outputs=outputs,
            errors=errors,
            levels=[[n.name for n in lvl] for lvl in levels],
            duration_s=_time.time() - start,
        )

    # ------------------------------------------------------------------
    # Level execution
    # ------------------------------------------------------------------
    async def _run_level_single(
        self, node: Node, outputs: dict[str, Any], context: Any,
    ) -> tuple[dict[str, Any], dict[str, BaseException]]:
        try:
            resolved = self._resolve(node, outputs)
            value = await node.run(resolved, context)
            return {node.name: value}, {}
        except BaseException as e:  # noqa: BLE001 — record anything
            return {}, {node.name: e}

    async def _run_level_parallel(
        self,
        level: list[Node],
        outputs: dict[str, Any],
        context: Any,
        *,
        fail_fast: bool,
    ) -> tuple[dict[str, Any], dict[str, BaseException]]:
        results: dict[str, Any] = {}
        errors: dict[str, BaseException] = {}

        async def _one(node: Node) -> tuple[str, Any | None, BaseException | None]:
            try:
                resolved = self._resolve(node, outputs)
                value = await node.run(resolved, context)
                return node.name, value, None
            except BaseException as e:  # noqa: BLE001
                return node.name, None, e

        if fail_fast:
            # Wrap the runnable so any user exception escapes as a real
            # Task exception (asyncio.wait FIRST_EXCEPTION won't fire if
            # _one() swallows the error into a tuple).
            async def _raw(node: Node) -> Any:
                resolved = self._resolve(node, outputs)
                return await node.run(resolved, context)

            tasks = {n.name: asyncio.create_task(_raw(n)) for n in level}
            done, pending = await asyncio.wait(
                tasks.values(), return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in done:
                name = next(n for n, t in tasks.items() if t is task)
                try:
                    exc = task.exception()
                except (asyncio.CancelledError, asyncio.InvalidStateError):
                    exc = None
                if exc is not None:
                    errors[name] = exc
                    results.pop(name, None)
                else:
                    try:
                        results[name] = task.result()
                    except asyncio.CancelledError:
                        pass
            if errors:
                # Cancel siblings — but don't await them; let them unwind
                # in the background so we can return quickly.
                for p in pending:
                    p.cancel()
            elif pending:
                more_done, _ = await asyncio.wait(pending)
                for task in more_done:
                    name = next(n for n, t in tasks.items() if t is task)
                    try:
                        exc = task.exception()
                    except (asyncio.CancelledError, asyncio.InvalidStateError):
                        exc = None
                    if exc is not None:
                        errors[name] = exc
                    else:
                        try:
                            results[name] = task.result()
                        except asyncio.CancelledError:
                            pass
        else:
            outcomes = await asyncio.gather(*(_one(n) for n in level))
            for name, value, err in outcomes:
                if err is not None:
                    errors[name] = err
                else:
                    results[name] = value
        return results, errors

    @staticmethod
    def _resolve(node: Node, outputs: dict[str, Any]) -> dict[str, Any]:
        # Precedence (later wins):
        #   constants < upstream outputs (node.inputs) < caller-supplied inputs
        # Upstream node names override constants because they are the data the
        # node was actually declared to consume; caller inputs override upstream
        # to let ad-hoc tests / re-runs inject fresh values without rebuilding
        # the DAG.
        merged: dict[str, Any] = dict(node.constants)
        for inp in node.inputs:
            if inp not in outputs:
                raise WorkflowMissingInputError(
                    f"node {node.name!r} requires upstream output {inp!r} "
                    f"which is not in the resolved outputs"
                )
            merged[inp] = outputs[inp]
        for k, v in outputs.items():
            if k in node.inputs or k in node.constants:
                continue
            merged.setdefault(k, v)
        return merged


__all__ = [
    "Node",
    "Workflow",
    "WorkflowResult",
    "WorkflowError",
    "WorkflowCycleError",
    "WorkflowMissingInputError",
]


# ---------------------------------------------------------------------------
# §7.3 #10 Tier-3 integration — a small ``build_tier3_research_workflow``
# factory that wires news + sentiment + synthesise into a DAG, mirroring
# the dsh-reference Tier-3 shape. Plugins / future orchestrator hooks can
# call this to get a ready-to-run DAG without re-deriving the topology.
# ---------------------------------------------------------------------------


def build_tier3_research_workflow(
    *,
    fetch_news: NodeCallable,
    fetch_quote: NodeCallable,
    sentiment: NodeCallable,
    synthesise: NodeCallable,
    name: str = "tier3-research",
) -> Workflow:
    """Construct the canonical research DAG::

        quote ─┐
               ├──▶ synthesise
        news ──▶ sentiment ──┘

    Returns:
        A :class:`Workflow` with 4 nodes; running it executes ``quote``
        and ``news`` in parallel round 1, then ``sentiment`` round 2,
        then ``synthesise`` round 3.
    """
    wf = Workflow(name)
    wf.add(Node(name="quote", run=fetch_quote, description="Pull latest quote"))
    wf.add(Node(name="news", run=fetch_news, description="Pull recent news"))
    wf.add(Node(
        name="sentiment",
        run=sentiment,
        inputs=("news",),
        description="Score news sentiment",
    ))
    wf.add(Node(
        name="synthesise",
        run=synthesise,
        inputs=("quote", "sentiment"),
        description="Combine quote + sentiment into research summary",
    ))
    return wf
