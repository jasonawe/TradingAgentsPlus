"""AgentRegistry V2 — descriptor-based registration + capability lookup.

Keeps V1 ``register(BaseAgent)`` for backwards compatibility while adding:
- ``register_v2(descriptor, factory)`` — descriptor-aware registration
- ``descriptor(name)`` — fetch the V2 descriptor
- ``find_capability(capability, scope)`` — deterministic sorted candidates
- ``handoff_metadata(name)`` — version/scope/priority/capabilities
- ``require(required_names)`` — startup check for planner / verifier / synthesizer
- ``record_legacy_agent(name, version)`` — health-error logger for V1 plugins
- ``health_errors()`` — recorded unsupported-version warnings
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .base import AgentDescriptor, BaseAgent


@dataclass
class _Registration:
    descriptor: AgentDescriptor
    factory: Callable[[], Any]
    instance: Any | None = None


class AgentRegistry:
    """Registry of V1 + V2 agents."""

    def __init__(self) -> None:
        self._v1: dict[str, BaseAgent] = {}
        self._v2: dict[str, _Registration] = {}
        self._health_errors: list[str] = []

    # ─── V1 registration (backwards compatible) ─────────────

    def register(self, agent: BaseAgent) -> None:
        if not agent.name:
            raise ValueError("agent must declare a non-empty name")
        if agent.name in self._v1:
            raise ValueError(f"agent {agent.name!r} already registered")
        self._v1[agent.name] = agent

    def get(self, name: str) -> Any:
        """Return the registered agent (V1 instance or V2 adapter).

        V2 entries are returned via ``LegacyAgentAdapter`` wrapping the
        factory output, so callers see a unified surface.
        """
        if name in self._v2:
            reg = self._v2[name]
            if reg.instance is None:
                reg.instance = reg.factory()
            return reg.instance
        if name in self._v1:
            return self._v1[name]
        raise KeyError(
            f"agent {name!r} not registered; known: {self.list()}"
        )

    def list(self) -> list[str]:
        names = set(self._v1) | set(self._v2)
        return sorted(names)

    def all(self) -> list[Any]:
        return [self.get(n) for n in self.list()]

    def has(self, name: str) -> bool:
        return name in self._v1 or name in self._v2

    # ─── V2 registration ────────────────────────────────────

    def register_v2(
        self,
        descriptor: AgentDescriptor,
        *,
        factory: Callable[[], Any] | None = None,
        instance: Any | None = None,
    ) -> None:
        """Register a V2 agent with descriptor + factory (or pre-built instance).

        Required names: ``planner``, ``verifier``, ``synthesizer`` for full
        AGENT_ANALYSIS coverage.
        """
        if not isinstance(descriptor, AgentDescriptor):
            raise TypeError("descriptor must be AgentDescriptor")
        if not descriptor.name:
            raise ValueError("descriptor.name must be non-empty")
        if descriptor.name in self._v2:
            raise ValueError(
                f"agent {descriptor.name!r} already registered"
            )
        if factory is None and instance is None:
            raise ValueError(
                "register_v2 requires either factory or instance"
            )
        if descriptor.version not in (1, 2):
            self._health_errors.append(
                f"agent {descriptor.name!r} registered with unsupported "
                f"version={descriptor.version}; expected 1 or 2"
            )
        self._v2[descriptor.name] = _Registration(
            descriptor=descriptor, factory=factory or (lambda: instance),
            instance=instance,
        )

    # ─── V2 queries ─────────────────────────────────────────

    def descriptor(self, name: str) -> AgentDescriptor | None:
        reg = self._v2.get(name)
        return reg.descriptor if reg else None

    def find_capability(
        self, capability: str, *, scope: str | None = None
    ) -> list[str]:
        """Return agent names that provide ``capability`` (optionally scoped).

        Sort key: ``(-priority, name)`` so callers can deterministically pick
        the first match.  ``scope=None`` matches any scope.
        """
        candidates: list[tuple[int, str, AgentDescriptor]] = []
        for name, reg in self._v2.items():
            d = reg.descriptor
            if capability not in d.capabilities:
                continue
            if scope is not None and d.scope != scope:
                continue
            candidates.append((d.priority, name, d))
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return [name for _, name, _ in candidates]

    def handoff_metadata(self, name: str) -> dict[str, Any]:
        """Return deterministic metadata for runtime handoff decisions.

        Includes: name, version, scope, priority, capabilities (sorted).
        """
        reg = self._v2.get(name)
        if reg is None:
            raise KeyError(f"agent {name!r} not registered (V2)")
        d = reg.descriptor
        return {
            "name": d.name,
            "version": d.version,
            "scope": d.scope,
            "priority": d.priority,
            "capabilities": sorted(d.capabilities),
        }

    # ─── startup checks ─────────────────────────────────────

    def require(self, required_names: list[str]) -> None:
        """Raise if any required agent is missing.

        Used at Harness startup to fail fast on missing planner / verifier
        / synthesizer.  Records the missing agent in ``health_errors``
        before raising so observability surfaces see it.
        """
        missing = [n for n in required_names if n not in self._v2 and n not in self._v1]
        if missing:
            for name in missing:
                self._health_errors.append(
                    f"required agent missing at startup: {name!r}"
                )
            raise RuntimeError(
                f"required agents missing: {missing}"
            )

    # ─── health errors ──────────────────────────────────────

    def record_legacy_agent(self, *, name: str, version: int) -> None:
        """Record a legacy (V1) agent registration as a health error.

        V1 agents still work via LegacyAgentAdapter, but their use is
        flagged so operators can plan a migration.
        """
        if version < 2:
            self._health_errors.append(
                f"legacy agent registered: {name!r} (version={version}); "
                f"V2 contract recommended"
            )

    def health_errors(self) -> list[str]:
        return list(self._health_errors)

    def plan_capabilities(self) -> list[dict[str, Any]]:
        """Return the union of all V1 agents' ``get_plan_steps()`` output.

        Used by the planner to enumerate available capabilities before
        constructing a PlanGraph.
        """
        out: list[dict[str, Any]] = []
        for agent in self._v1.values():
            try:
                steps = agent.get_plan_steps()
            except Exception:
                continue
            for step in steps or []:
                out.append(dict(step))
        return out


__all__ = ["AgentRegistry"]
