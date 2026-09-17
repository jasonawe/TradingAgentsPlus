"""Plugin ABC (v3 spec §5.4).

Third-party plugins register through ``pyproject.toml``::

    [project.entry-points."agent_harness.plugins"]
    my_plugin = "my_pkg.plugin:MyPlugin"
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

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

    def install(self, harness: "Harness") -> None:
        """Install hook — called by ``PluginRegistry.register(p)``.

        Default implementation:
        - register tools (idempotent)
        - register agents (idempotent — N63 fix: plugins return REFERENCES
          to core agents, not new instances)
        - inject ``prompts()`` output into ``harness.system_prompt_waterfall``
          as :class:`WaterfallSection` entries (§7.1 #7). Previously this
          data was dropped into ``harness._prompt_*`` attributes that
          nothing read — the waterfall now composes them into the final
          system prompt, so a plugin's persona / template actually shows up.

        Subclasses may override for plugin-specific setup (network pools,
        extra waterfall sections, lifecycle hooks, etc.).
        """
        from tradingagents.agent_harness.tools.base import BaseTool as _BT
        from tradingagents.agent_harness.core.system_prompt_waterfall import (
            WaterfallSection,
        )

        for t in self.tools():
            if isinstance(t, _BT) and t.name not in harness.tool_registry.list_names():
                harness.tool_registry.add(t)
        for a in self.agents():
            # Idempotent: only register if not already present (N63 fix — plugins
            # return REFERENCES to core agents, not new instances).
            if a.name not in harness.agent_registry.list():
                harness.agent_registry.register(a)
        # prompts() → real waterfall sections (§7.1 #7). Re-install with the
        # same plugin replaces the prior section so a plugin that updates
        # its prompt content gets refreshed on reinstall. Name collisions
        # between two plugins resolve by waterfall priority (default 50
        # via prompt_priority(); later install wins on equal priority).
        waterfall = getattr(harness, "system_prompt_waterfall", None)
        if waterfall is None:
            return  # harness created without waterfall — legacy / test path
        priority = self.prompt_priority()
        for prompt_name, content in self.prompts().items():
            section_name = f"plugin:{self.name}:{prompt_name}"
            waterfall.remove(section_name)
            waterfall.add(WaterfallSection(
                name=section_name,
                content=content,
                source=self.name,
                priority=priority,
            ))

    def prompt_priority(self) -> int:
        """Priority for ``prompts()`` sections inside the system-prompt waterfall.

        Default 50 (matches WaterfallSection default). Plugins can override
        to push critical personas higher (e.g. a risk-policy plugin might
        want priority 80) or push cosmetic content lower.
        """
        return 50
