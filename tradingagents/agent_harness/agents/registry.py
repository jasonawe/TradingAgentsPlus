"""AgentRegistry stub — P5 will replace this with full implementation."""
from __future__ import annotations

from typing import Any


class AgentRegistry:
    """Minimal stub used by Harness/Orchestrator; replaced in P5."""

    def __init__(self) -> None:
        self._agents: dict[str, Any] = {}

    def register(self, agent: Any) -> None:
        if agent.name in self._agents:
            raise ValueError(f"agent {agent.name!r} already registered")
        self._agents[agent.name] = agent

    def get(self, name: str) -> Any:
        if name not in self._agents:
            raise KeyError(f"agent {name!r} not registered; known: {sorted(self._agents)}")
        return self._agents[name]

    def list(self) -> list[str]:
        return sorted(self._agents)

    def plan_capabilities(self) -> list[dict]:
        return [
            {"agent": a.name, "capability": getattr(a, "description", "")}
            for a in self._agents.values()
        ]
