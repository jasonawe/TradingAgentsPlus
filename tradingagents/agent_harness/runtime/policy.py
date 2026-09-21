"""Task 10 — PolicyGuard and bounded GraphPatch.

PolicyGuard 是纯决策层 — 不直接执行任何持久化或副作用。
RuntimeStore 单独负责应用 approved patch transaction。

核心契约:
- validate_initial_graph: 校验初始 PlanGraph(agent / capability / max_tasks /
  cycle / handoff_depth)
- select_handoff_agent: 按 (capability, scope, -priority, name) 确定性选 agent
- build_patch_from_message: 只接受 HANDOFF_REQUEST / REPAIR_REQUEST
- authorize_patch: 校验 patch 的 agent/capability/graph_revision/ancestor 排除
"""
from __future__ import annotations

from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import AgentMessageDraft, HandoffPayload, RepairPayload


# ════════════════════════════════════════════════════════
# Pydantic base
# ════════════════════════════════════════════════════════


class _FrozenModel(BaseModel):
    """Frozen Pydantic base for policy value objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ════════════════════════════════════════════════════════
# Registry — registered agents and their capabilities
# ════════════════════════════════════════════════════════


class _Agent:
    """Internal registered agent descriptor.

    PolicyGuard 不假设 agent 必须继承任何基类 — 任何带 ``name`` /
    ``capabilities`` / ``scope`` / ``priority`` 属性的对象都可以注册。
    """


class AgentRegistry:
    """Registry of agents the runtime may invoke.

    Agents are duck-typed: any object exposing ``name`` (str), ``capabilities``
    (Iterable[str]), ``scope`` (str) and optional ``priority`` (int) is
    acceptable.  ``register()`` records the object; ``by_capability_and_scope``
    returns candidates matching both the capability tag and the requested
    scope, sorted by ``(-priority, name)`` for deterministic selection.
    """

    def __init__(self) -> None:
        self._agents: dict[str, Any] = {}

    # ─── registration ────────────────────────────────────────

    def register(self, agent: Any) -> None:
        """Register ``agent`` by its ``name`` attribute.

        Raises ``ValueError`` if the agent lacks a name or if the same name
        has already been registered.
        """
        name = getattr(agent, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError("agent must expose non-empty string `name`")
        if name in self._agents:
            raise ValueError(f"agent already registered: {name!r}")
        self._agents[name] = agent

    # ─── inspection ──────────────────────────────────────────

    @property
    def agents(self) -> list[Any]:
        """Return all registered agents in registration order."""
        return list(self._agents.values())

    def get(self, name: str) -> Any | None:
        """Return the registered agent with ``name`` (or ``None``)."""
        return self._agents.get(name)

    def names(self) -> list[str]:
        """Return all registered agent names (insertion order)."""
        return list(self._agents.keys())

    # ─── capability / scope queries ──────────────────────────

    def by_capability_and_scope(
        self, capability: str, scope: str
    ) -> list[Any]:
        """Return agents matching ``capability`` AND ``scope``.

        The result is sorted by ``(-priority, name)`` so callers can pick the
        deterministic first match.  ``capability == "*"`` matches any
        capability; ``scope == "*"`` matches any scope (used by tests that
        need a forgiving match).
        """
        cap = capability
        scope_want = scope
        matches: list[Any] = []
        for agent in self._agents.values():
            caps = set(getattr(agent, "capabilities", []) or [])
            agent_scope = getattr(agent, "scope", "")
            if cap != "*" and cap not in caps:
                continue
            if scope_want != "*" and agent_scope != scope_want:
                continue
            matches.append(agent)
        matches.sort(
            key=lambda a: (
                -int(getattr(a, "priority", 0)),
                str(getattr(a, "name", "")),
            )
        )
        return matches


# ════════════════════════════════════════════════════════
# Plan shape — minimal graph types for initial validation
# ════════════════════════════════════════════════════════


class PlanNode(_FrozenModel):
    """A single node in the initial PlanGraph.

    Deliberately minimal — the planner emits richer ``PlanTask`` instances to
    the runtime; PolicyGuard only needs enough to validate topology.
    """

    task_key: str
    agent: str
    capability: str
    depends_on: list[str] = Field(default_factory=list)


class PlanGraph(_FrozenModel):
    """Container for the initial plan that ``validate_initial_graph`` accepts.

    Distinct from ``models.PlanGraph`` (which carries full ``PlanTask`` +
    budgets) — PolicyGuard's view is the topology-only validation surface.
    """

    nodes: list[PlanNode]


# ════════════════════════════════════════════════════════
# Patch shape — bounded GraphPatch envelope used by the guard
# ════════════════════════════════════════════════════════


class PatchAddTask(_FrozenModel):
    """Atomic ``ADD_TASK`` operation embedded in a GraphPatch.

    Distinct from ``models.AddTaskOp`` — the policy view carries ``on_reject``
    and ``depends_on`` directly so the guard can reason about requestor
    intent without re-reading the runtime schema.
    """

    task_key: str
    agent: str
    capability: str
    objective: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    required: bool = True
    on_reject: Literal["FAIL_REQUESTER", "RESUME_REQUESTER"] | None = None
    depends_on: list[str] = Field(default_factory=list)


class GraphPatch(_FrozenModel):
    """Bounded GraphPatch envelope validated by ``authorize_patch``.

    Distinct from ``models.GraphPatch`` — PolicyGuard does not need the full
    store-side envelope (``patch_id`` / ``run_id`` / ``reason_code`` /
    operations list).  The guard only needs the graph revision it was built
    against and a single ADD_TASK atom (the plan calls out a single bounded
    patch per dispatched message).
    """

    graph_revision: int = Field(ge=0)
    add_task: PatchAddTask
    requester_task_id: str | None = None


# ════════════════════════════════════════════════════════
# PolicyDecision — typed decision value object
# ════════════════════════════════════════════════════════


class PolicyDecision(_FrozenModel):
    """Typed result from every PolicyGuard method.

    ``reason_code`` is an empty string when ``ok=True``; callers compare
    against the canonical codes listed in ``policy.py`` (unknown_agent,
    capability_mismatch, max_tasks_exceeded, graph_cycle,
    handoff_depth_exceeded, stale_graph_revision, ancestor_loop,
    unsupported_message_type, no_candidate_agent, ...).
    """

    ok: bool
    reason_code: str = ""
    retry: bool = False
    on_reject_action: Literal["FAIL_REQUESTER", "RESUME_REQUESTER"] | None = None

    @model_validator(mode="after")
    def _ok_has_no_reason(self) -> PolicyDecision:
        if self.ok and self.reason_code:
            raise ValueError("ok=True decisions must not carry a reason_code")
        return self


# ════════════════════════════════════════════════════════
# PolicyGuard
# ════════════════════════════════════════════════════════


class PolicyGuard:
    """Pure decision layer for the supervised AgentRuntime.

    Holds the agent registry plus budget knobs.  All four entry points return
    a ``PolicyDecision``; none of them touch storage or call LLM/tooling.
    RuntimeStore is the only component allowed to apply an approved patch
    transaction.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        max_tasks: int = 10,
        max_handoff_depth: int = 3,
        max_messages: int = 100,
        max_repairs: int = 2,
        deadline_seconds: float = 600.0,
        max_tokens: int = 4000,
    ) -> None:
        self.registry = registry
        self.max_tasks = max_tasks
        self.max_handoff_depth = max_handoff_depth
        self.max_messages = max_messages
        self.max_repairs = max_repairs
        self.deadline_seconds = deadline_seconds
        self.max_tokens = max_tokens

    # ─── helpers ─────────────────────────────────────────────

    def _agent(self, name: str) -> Any | None:
        return self.registry.get(name)

    def _has_capability(self, agent: Any, capability: str) -> bool:
        caps = set(getattr(agent, "capabilities", []) or [])
        return capability in caps

    # ─── validate_initial_graph ─────────────────────────────

    def validate_initial_graph(self, graph: PlanGraph) -> PolicyDecision:
        """Validate a planner-emitted ``PlanGraph`` before the runtime starts.

        Checks (in order):
        1. ``len(nodes) <= max_tasks``
        2. each node's agent is registered
        3. each node's agent exposes the requested capability
        4. dependency graph is acyclic (DFS cycle detection)
        5. longest dependency chain depth ≤ ``max_handoff_depth``
        """
        nodes = list(graph.nodes)

        # 1. max_tasks
        if len(nodes) > self.max_tasks:
            return PolicyDecision(
                ok=False, reason_code="max_tasks_exceeded"
            )

        # 2/3. agent registered + capability match
        for node in nodes:
            agent = self._agent(node.agent)
            if agent is None:
                return PolicyDecision(
                    ok=False,
                    reason_code=f"unknown_agent:{node.agent}",
                )
            if not self._has_capability(agent, node.capability):
                return PolicyDecision(
                    ok=False,
                    reason_code=(
                        f"capability_mismatch:{node.agent}:{node.capability}"
                    ),
                )

        # 4. cycle detection (iterative DFS)
        node_index = {n.task_key: n for n in nodes}
        cycle = self._find_cycle(node_index)
        if cycle is not None:
            return PolicyDecision(
                ok=False, reason_code="graph_cycle"
            )

        # 5. handoff depth
        depth = self._longest_chain(node_index)
        if depth > self.max_handoff_depth:
            return PolicyDecision(
                ok=False, reason_code="handoff_depth_exceeded"
            )

        return PolicyDecision(ok=True)

    @staticmethod
    def _find_cycle(node_index: dict[str, PlanNode]) -> list[str] | None:
        """Return the cycle path if any (else ``None``).

        Iterative three-color DFS to keep memory bounded on large graphs.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {k: WHITE for k in node_index}
        parent: dict[str, str | None] = {k: None for k in node_index}

        for root in list(node_index.keys()):
            if color[root] != WHITE:
                continue
            stack: list[tuple[str, Iterable[str]]] = [
                (root, list(node_index[root].depends_on))
            ]
            color[root] = GRAY
            while stack:
                node, children = stack[-1]
                if not children:
                    color[node] = BLACK
                    stack.pop()
                    continue
                nxt = children[0]
                remaining = children[1:]
                stack[-1] = (node, remaining)
                if nxt not in color:
                    # dangling edge to a non-existent task — treat as cycle
                    # candidate so the planner cannot smuggle in references.
                    return [node, nxt]
                if color[nxt] == GRAY:
                    # cycle found; reconstruct path from nxt → ... → node
                    path = [nxt]
                    cursor: str | None = node
                    while cursor is not None and cursor != nxt:
                        path.append(cursor)
                        cursor = parent[cursor]
                    path.append(nxt)
                    return list(reversed(path))
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    parent[nxt] = node
                    stack.append(
                        (nxt, list(node_index[nxt].depends_on))
                    )
        return None

    @staticmethod
    def _longest_chain(node_index: dict[str, PlanNode]) -> int:
        """Return the longest path length (in edges) through the DAG.

        Iterative memoised DFS — bounded by ``len(node_index)``.
        """
        depth_cache: dict[str, int] = {}

        def depth(key: str, stack: set[str]) -> int:
            if key in depth_cache:
                return depth_cache[key]
            if key in stack:
                return 0
            node = node_index.get(key)
            if node is None:
                return 0
            stack.add(key)
            try:
                best = 0
                for dep in node.depends_on:
                    best = max(best, depth(dep, stack) + 1)
            finally:
                stack.discard(key)
            depth_cache[key] = best
            return best

        overall = 0
        for key in node_index:
            overall = max(overall, depth(key, set()))
        return overall

    # ─── select_handoff_agent ────────────────────────────────

    def select_handoff_agent(
        self, capability: str, scope: str
    ) -> str | None:
        """Return the deterministic best agent name for ``capability`` /
        ``scope``.

        Sort key is ``(-priority, name)`` so:
        - higher priority wins
        - name ties break alphabetically (stable across runs)
        Returns ``None`` if no candidate matches.
        """
        candidates = self.registry.by_capability_and_scope(capability, scope)
        if not candidates:
            return None
        return getattr(candidates[0], "name")

    # ─── build_patch_from_message ────────────────────────────

    def build_patch_from_message(
        self,
        draft: AgentMessageDraft,
        *,
        requester_task_id: str,
        graph_revision: int,
    ) -> GraphPatch | None:
        """Translate a HANDOFF_REQUEST / REPAIR_REQUEST draft into a patch.

        Returns ``None`` for any other draft type (QUESTION / PROGRESS / etc.)
        — the runtime layer must not produce patches for unsolicited chatter.
        The selected agent name is resolved through the registry using the
        payload's ``required_capability`` and the requester's scope (the
        requester is unknown to the guard here; pass the actual requester
        scope via ``requester_scope`` in future revisions when planner-side
        context becomes available).
        """
        payload = draft.payload
        if isinstance(payload, HandoffPayload):
            scope = self._requester_scope_from_draft(draft)
            agent_name = self.select_handoff_agent(
                payload.required_capability, scope
            )
            if agent_name is None:
                # Try global scope as a fallback — the planner may emit
                # user-scoped handoffs into the global agent pool.
                agent_name = self.select_handoff_agent(
                    payload.required_capability, "global"
                )
            if agent_name is None:
                return None
            return GraphPatch(
                graph_revision=graph_revision,
                add_task=PatchAddTask(
                    task_key=f"{requester_task_id}:handoff:"
                    f"{payload.required_capability}",
                    agent=agent_name,
                    capability=payload.required_capability,
                    objective=payload.objective,
                    inputs={"requested_capability": payload.required_capability},
                    required=True,
                    on_reject=payload.on_reject,
                ),
                requester_task_id=requester_task_id,
            )

        if isinstance(payload, RepairPayload):
            # Repairs reuse the same capability that produced the original
            # artifact — recover it from the requester's last known capability
            # when available; otherwise fall back to the registry's first
            # agent that accepts the domain evidence repair kind.
            capability = self._capability_for_repair(payload)
            agent_name = self.select_handoff_agent(capability, "global")
            if agent_name is None:
                return None
            return GraphPatch(
                graph_revision=graph_revision,
                add_task=PatchAddTask(
                    task_key=f"{requester_task_id}:repair:{payload.target_task_id}",
                    agent=agent_name,
                    capability=capability,
                    objective=(
                        f"repair {payload.target_task_id} "
                        f"({payload.repair_kind})"
                    ),
                    inputs={
                        "requested_capability": capability,
                        "repair_kind": payload.repair_kind,
                        "missing_evidence": list(payload.missing_evidence),
                        "rejected_artifact_ids": list(
                            payload.rejected_artifact_ids
                        ),
                    },
                    required=True,
                    on_reject=payload.on_reject,
                ),
                requester_task_id=requester_task_id,
            )

        return None

    def _requester_scope_from_draft(self, draft: AgentMessageDraft) -> str:
        """Infer the requester's scope from the draft's recipient.

        The runtime persists sender scope on persisted ``AgentMessage``;
        ``AgentMessageDraft`` only carries the recipient.  When the
        recipient is a known agent, its ``scope`` is a reasonable proxy for
        "scope the requester is allowed to escalate into".
        """
        recipient = self.registry.get(draft.recipient)
        if recipient is not None:
            scope = getattr(recipient, "scope", None)
            if isinstance(scope, str) and scope:
                return scope
        return "user"

    @staticmethod
    def _capability_for_repair(payload: RepairPayload) -> str:
        """Map a RepairPayload to the capability required to repair it.

        DOMAIN_EVIDENCE is produced by the original domain agent; SYNTHESIS
        is a planner-level concern.  This mapping is intentionally simple —
        richer capability resolution lives in the planner.
        """
        if payload.repair_kind == "DOMAIN_EVIDENCE":
            return "domain_lookup"
        return "synthesis"

    # ─── authorize_patch ─────────────────────────────────────

    def authorize_patch(
        self,
        patch: GraphPatch,
        *,
        current_graph_revision: int | None = None,
        ancestor_chain: set[str] | None = None,
    ) -> PolicyDecision:
        """Authorize (or reject) a GraphPatch proposed by the runtime.

        Checks:
        1. ``add_task.agent`` is registered
        2. ``add_task.agent`` exposes ``add_task.capability``
        3. if ``current_graph_revision`` provided → must equal
           ``patch.graph_revision`` (else ``stale_graph_revision`` + retry)
        4. ``add_task.depends_on`` must not intersect ``ancestor_chain`` when
           provided (else ``ancestor_loop``)
        """
        add = patch.add_task

        agent = self._agent(add.agent)
        if agent is None:
            return PolicyDecision(ok=False, reason_code="unknown_agent")
        if not self._has_capability(agent, add.capability):
            return PolicyDecision(
                ok=False, reason_code="capability_mismatch"
            )

        if (
            current_graph_revision is not None
            and patch.graph_revision != current_graph_revision
        ):
            return PolicyDecision(
                ok=False,
                reason_code="stale_graph_revision",
                retry=True,
            )

        if ancestor_chain is not None:
            loop_hits = set(add.depends_on) & set(ancestor_chain)
            if loop_hits:
                return PolicyDecision(
                    ok=False,
                    reason_code="ancestor_loop",
                )

        return PolicyDecision(
            ok=True, on_reject_action=add.on_reject
        )


__all__ = [
    "AgentRegistry",
    "GraphPatch",
    "PatchAddTask",
    "PlanGraph",
    "PlanNode",
    "PolicyDecision",
    "PolicyGuard",
]
