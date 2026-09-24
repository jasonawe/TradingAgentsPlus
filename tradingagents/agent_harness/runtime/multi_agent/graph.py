"""Core graph types for the multi-agent runtime (Phase 1).

Edge validates `kind` in __post_init__ and tracks `hops_used` for per-edge
loop accounting. The GraphExecutor resets every edge's `hops_used = 0` at
the top of each `run()` so the same executor+spec pair can run multiple
times (rev.5 fix from round-4 HIGH #1).

`max_hops` default is 2 per spec §4.6 step 4.

Rev.5: ``GraphSpec.to_dict()`` MUST be derived from immutable fields only —
do NOT include ``Edge.hops_used`` in any future cache key (see MEDIUM #2).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable
import json

from .state import FieldRef, Message, TypedResult

class NodeKind(str, Enum):
    TOOL = "tool"
    LLM = "llm"
    SUBPLAN = "subplan"
    CONSULT = "consult"

_VALID_EDGE_KINDS = ("data", "when", "loop")

@runtime_checkable
class BaseNode(Protocol):
    id: str
    agent_id: str
    kind: NodeKind
    inputs: list[FieldRef]
    outputs: list[FieldRef]
    async def run(self, state, inbox): ...

@dataclass
class Edge:
    src: str
    dst: str
    kind: str
    field_ref: Optional[str] = None
    predicate: Optional[Any] = None
    max_hops: int = 2  # spec §4.6 step 4
    hops_used: int = 0  # executor resets to 0 at top of each run()

    def __post_init__(self) -> None:
        if self.kind not in _VALID_EDGE_KINDS:
            raise ValueError(
                f"invalid edge kind: {self.kind!r} (must be one of {_VALID_EDGE_KINDS})"
            )

@dataclass
class GraphSpec:
    nodes: dict[str, BaseNode]
    edges: list[Edge] = field(default_factory=list)
    entry: Optional[str] = None
    exit: Optional[str] = None

    def node(self, node_id: str) -> BaseNode:
        return self.nodes[node_id]

    def edges_from(self, src: str) -> list[Edge]:
        return [e for e in self.edges if e.src == src]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable snapshot.

        NOTE (rev.5 MEDIUM #2): ``hops_used`` is mutable state mutated by the
        executor. Do NOT use ``to_dict()`` as a cache key — derive cache keys
        from immutable parts only (intent, frozenset(nodes), plan source hash).
        """
        return {
            "nodes": {
                nid: {"id": n.id, "agent_id": n.agent_id, "kind": n.kind.value}
                for nid, n in self.nodes.items()
            },
            "edges": [
                {"src": e.src, "dst": e.dst, "kind": e.kind,
                 "field_ref": e.field_ref, "max_hops": e.max_hops,
                 "hops_used": e.hops_used}
                for e in self.edges
            ],
            "entry": self.entry,
            "exit": self.exit,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
