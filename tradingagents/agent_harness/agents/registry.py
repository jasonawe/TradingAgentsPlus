"""AgentRegistry (v3 spec §4.3).

Adding a new agent = one ``register(agent)`` line; Harness core stays
unchanged (N64 fix: agents are registered directly on AgentRegistry,
not via Plugin.agents()).
"""
from __future__ import annotations

from .base import BaseAgent


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        if not agent.name:
            raise ValueError("agent must declare a non-empty name")
        if agent.name in self._agents:
            raise ValueError(f"agent {agent.name!r} already registered")
        self._agents[agent.name] = agent

    def get(self, name: str) -> BaseAgent:
        if name not in self._agents:
            raise KeyError(f"agent {name!r} not registered; known: {sorted(self._agents)}")
        return self._agents[name]

    def list(self) -> list[str]:
        return sorted(self._agents)

    def all(self) -> list[BaseAgent]:
        return list(self._agents.values())

    def plan_capabilities(self) -> list[dict]:
        caps: list[dict] = []
        for a in self._agents.values():
            caps.extend(a.get_plan_steps())
        return caps
