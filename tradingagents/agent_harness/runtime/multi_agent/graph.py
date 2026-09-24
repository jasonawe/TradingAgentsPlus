"""Core graph types for the multi-agent runtime (Phase 2).

Edge validates `kind` in __post_init__. Per-edge loop accounting lives on
``state.edge_hops[(src, dst)]`` (Phase 2 Work unit 5, spec §4.7); the
executor resets ``state.edge_hops = {}`` at the top of every ``run()``
so the same executor+spec pair can run multiple times without leaking
state across turns.

`max_hops` default is 2 per spec §4.6 step 4.

Rev.5: ``GraphSpec.to_dict()`` is derived from immutable fields only —
safe to use as a cache key (no mutable state mutated by the executor).
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

        All fields are immutable post-construction (Phase 2 Work unit 5
        removed ``Edge.hops_used``), so this snapshot is safe to use as a
        cache key (spec §4.7 strict). Per-run counters live on
        ``state.edge_hops``, not on Edge instances.
        """
        return {
            "nodes": {
                nid: {"id": n.id, "agent_id": n.agent_id, "kind": n.kind.value}
                for nid, n in self.nodes.items()
            },
            "edges": [
                {"src": e.src, "dst": e.dst, "kind": e.kind,
                 "field_ref": e.field_ref, "max_hops": e.max_hops}
                for e in self.edges
            ],
            "entry": self.entry,
            "exit": self.exit,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
