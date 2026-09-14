"""Plugin ABC (v3 spec §5.4).

Third-party plugins register through ``pyproject.toml``::

    [project.entry-points."agent_harness.plugins"]
    my_plugin = "my_pkg.plugin:MyPlugin"
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from tradingagents.agent_harness.agents.base import BaseAgent
    from tradingagents.agent_harness.harness import Harness
    from tradingagents.agent_harness.tools.base import BaseTool


class Plugin(ABC):
    name: str
    version: str
    description: str = ""

    @abstractmethod
    def tools(self) -> list["BaseTool"]:
        ...

    def agents(self) -> list["BaseAgent"]:
        """Optional: return references to core agents (N63 fix — no double impl)."""
        return []

    def prompts(self) -> dict[str, str]:
        """Optional: override default prompts (name → content)."""
        return {}

    def config_schema(self) -> type[BaseModel] | None:
        """Optional: plugin config schema validated by HarnessConfig."""
        return None

    @abstractmethod
    def install(self, harness: "Harness") -> None:
        """Install hook — called by ``PluginRegistry.register(p)``.

        Default implementation: register tools + agents (idempotent) + prompts.
        Subclasses may override for plugin-specific setup (network pools, etc.).
        """
        from tradingagents.agent_harness.tools.base import BaseTool as _BT

        for t in self.tools():
            if isinstance(t, _BT) and t.name not in harness.tool_registry.list_names():
                harness.tool_registry.add(t)
        for a in self.agents():
            # Idempotent: only register if not already present (N63 fix — plugins
            # return REFERENCES to core agents, not new instances).
            if a.name not in harness.agent_registry.list():
                harness.agent_registry.register(a)
        prompts = self.prompts()
        for prompt_name, content in prompts.items():
            setattr(harness, f"_prompt_{prompt_name}", content)
