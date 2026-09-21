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

    def __init__(self, orchestrator: object | None = None) -> None:
        # orchestrator param kept for backwards compat with V1 callers
        # (web/app.py, legacy tests). V2 does not bind handlers to it —
        # see `list()`/`get_yaml_text()`/`reload()` for the V1
        # compatibility shims that return empty / not-found.
        self._specs: dict[str, WorkflowSpec] = {}
        self._orchestrator = orchestrator

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

    # ---- V1 backwards-compat shims (Task 21 removed YAML/handler
    # binding, but web/app.py and tests still call these). ----

    def list(self) -> list[dict]:
        """V1 shim — return registered specs as dicts.

        V2 only carries dataclass WorkflowSpec entries, so each row
        gets a synthetic ``id`` (== name) and empty label. YAML specs
        (file-loaded) are gone in V2, so the result is always []
        unless callers explicitly ``register()`` dataclass specs.
        """
        return [
            {"id": s.name, "label": s.name, "name": s.name}
            for s in self._specs.values()
        ]

    def get_yaml_text(self, name: str) -> str | list[str]:
        """V1 shim — V2 has no YAML backing; return not-found error list."""
        if name in self._specs:
            return f"# V2 dataclass spec {name!r} has no YAML text\n"
        return [f"V1 YAML workflow {name!r} is no longer supported (Task 21 moved to V2 dataclass registry)"]

    def reload(self, name: str) -> object | list[str]:
        """V1 shim — V2 has no YAML reload; return not-found error list."""
        return [f"V1 YAML workflow {name!r} reload is no longer supported (Task 21 moved to V2 dataclass registry)"]

    @property
    def orchestrator(self) -> object | None:
        """V1 shim — expose orchestrator reference if provided at construction."""
        return self._orchestrator


__all__ = ["WorkflowSpec", "WorkflowSpecRegistry"]
