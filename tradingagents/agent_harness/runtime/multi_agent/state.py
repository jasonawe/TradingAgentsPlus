"""Turn-level ephemeral state for the multi-agent runtime (Phase 1).

Rev.5 contract: ``TypedResult.meta`` reserves three keys per spec §7:
``source_ts`` (staleness), ``source_agent``, ``source_tool``. Phase 4 verifier
reads ``source_ts`` to reject stale outputs. Phase 1 producers SHOULD populate
``source_ts`` but it's not enforced.

Rev.5 bootstrap doc: ``state.inbox[entry]`` is **empty** after bootstrap
(see executor.py docstring). Entry nodes must use ``self.raw_args`` for Phase 1
input. Phase 3 will wire ``\$ref`` resolution; entry-node ``\$ref`` may produce
stale/missing inputs until ``state.agent_outputs`` population lands.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Literal

@dataclass(frozen=True)
class FieldRef:
    agent: str
    field: str

    def __str__(self) -> str:
        return f"\${self.agent}.{self.field}"

@dataclass
class TypedResult:
    schema: type
    data: Any
    meta: dict

MessageKind = Literal["data", "question", "answer", "delta"]

@dataclass
class Message:
    sender: str
    receiver: str
    payload: TypedResult
    kind: MessageKind = "data"
    ts: float = field(default_factory=time.monotonic)
    hop: int = 0

@dataclass
class GraphState:
    run_id: str
    turn_id: str
    intent: str
    # NOTE: spec §4.1 also lists `symbols` and `plan_id`; both land in Phase 4.
    agent_outputs: dict[str, TypedResult] = field(default_factory=dict)
    inbox: dict[str, list[Message]] = field(default_factory=dict)
    message_log: list[Message] = field(default_factory=list)
    hops_remaining: int = 8
    llm_used: int = 0
    consultation_used: int = 0
    # §0.4.35 phase 2 (Work unit 4) — tracks live consult nesting so the
    # executor can refuse ConsultNode invocations that would push the
    # graph past ``RuntimeSettings.consultation_max_depth`` (default 3).
    # Per-turn state means this counter auto-resets across turns.
    consultation_depth: int = 0
    # NOTE: `consultation_rate_limit` is dead in Phase 1 — Phase 2 enforces
    # the "no nested consult" rule inside GraphExecutor._invoke (Out of Scope).
    budget_limit: int = 5
    consultation_rate_limit: float = 0.5
    # Phase 2 Work unit 3 — LLMProvider handle for LLMNode.run().
    # Set by the orchestrator before GraphExecutor.run() when the runtime
    # multi_agent flag is on. Defaults to None so Phase 1 graphs run
    # unchanged; LLMNode.run() raises NotImplementedError if None (executor
    # catches + logs warning, llm_used unchanged — Phase 1 wiring tests
    # continue to pass).
    llm_provider: Any = None
    # Phase 2 Work unit 5 — per-edge loop counters live on state, NOT on
    # Edge instances (spec §4.7). The executor resets this to ``{}`` at the
    # top of every ``run()`` so the same executor+spec pair can run
    # multiple times without leaking state across turns.
    #
    # Key = (src, dst); Value = number of times the edge has fired.
    # Tuple keys are hashable + comparable; Phase 3 may add an edge_kind
    # dimension (data/when/loop) if same-(src,dst) tuples with different
    # semantics appear in the same spec.
    edge_hops: dict[tuple[str, str], int] = field(default_factory=dict)
    # §0.4.35 phase 4 (Work unit 1) — sub-plan nesting guard.
    # ``subplan_depth`` increments each time SubplanNode.run forks the
    # state; when it reaches ``subplan_max_depth``, SubplanNode refuses
    # with a ``SubplanDepthExceeded`` TypedResult. Per-turn state means
    # this counter auto-resets across turns. Mirrors the consultation
    # depth/rate-limit pattern from Phase 2.
    subplan_depth: int = 0
    # Default mirrors ``RuntimeSettings.subplan_max_depth`` (3). The
    # orchestrator's ``_build_graph_state`` plumbs the user-configured
    # value in via the kwarg — see ``core/orchestrator.py``.
    subplan_max_depth: int = 3

    def append_inbox(self, msg: Message) -> None:
        self.inbox.setdefault(msg.receiver, []).append(msg)
        self.message_log.append(msg)

    def consume_inbox(self, receiver: str) -> list[Message]:
        return self.inbox.pop(receiver, [])

    def fork(self) -> "GraphState":
        """Phase 4 WU1: shallow copy with deep-copied mutable fields.

        Returns an independent ``GraphState`` suitable for ``SubplanNode``
        to execute a sub-graph in isolation — mutations on the fork do
        NOT affect the parent (and vice versa). Scalars (run_id, turn_id,
        intent, counters) share the same value; structural primitives
        (dicts / lists of TypedResult / Message) are deep-copied.

        INVARIANT: ``TypedResult.data`` is treated as opaque — caller-
        provided dicts/lists are deep-copied so the fork can mutate
        nested structures without leaking into the parent.
        """
        import copy as _copy
        from dataclasses import replace
        return replace(
            self,
            agent_outputs={
                k: TypedResult(
                    schema=v.schema,
                    data=_copy.deepcopy(v.data),
                    meta=dict(v.meta),
                )
                for k, v in self.agent_outputs.items()
            },
            inbox={
                recv: [
                    Message(
                        sender=m.sender,
                        receiver=m.receiver,
                        payload=TypedResult(
                            schema=m.payload.schema,
                            data=_copy.deepcopy(m.payload.data),
                            meta=dict(m.payload.meta),
                        ),
                        kind=m.kind,
                        ts=m.ts,
                        hop=m.hop,
                    )
                    for m in msgs
                ]
                for recv, msgs in self.inbox.items()
            },
            message_log=[
                Message(
                    sender=m.sender,
                    receiver=m.receiver,
                    payload=TypedResult(
                        schema=m.payload.schema,
                        data=_copy.deepcopy(m.payload.data),
                        meta=dict(m.payload.meta),
                    ),
                    kind=m.kind,
                    ts=m.ts,
                    hop=m.hop,
                )
                for m in self.message_log
            ],
            edge_hops=dict(self.edge_hops),
        )
