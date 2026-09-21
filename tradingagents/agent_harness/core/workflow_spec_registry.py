"""Task 21 — WorkflowSpecRegistry.

Lightweight registry for V2 workflow specs. Stores dataclass-style specs
with steps, required agents, and timeout.  Backed by an in-process dict
(not persisted; workflow specs ship with the harness).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WorkflowSpec:
    """Static workflow specification."""

    name: str
    steps: tuple[dict, ...]
    required_agents: tuple[str, ...]
    timeout_seconds: float = 60.0
    metadata: dict = field(default_factory=dict)


class WorkflowSpecRegistry:
    """In-process registry of named WorkflowSpec entries."""

    def __init__(self) -> None:
        self._specs: dict[str, WorkflowSpec] = {}

    def register(self, spec: WorkflowSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"workflow spec already registered: {spec.name!r}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> WorkflowSpec:
        if name not in self._specs:
            raise KeyError(f"workflow spec not found: {name!r}")
        return self._specs[name]

    def list_names(self) -> list[str]:
        return sorted(self._specs)

    def has(self, name: str) -> bool:
        return name in self._specs


__all__ = ["WorkflowSpec", "WorkflowSpecRegistry"]
