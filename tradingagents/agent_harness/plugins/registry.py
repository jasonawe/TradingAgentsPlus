"""PluginRegistry + entry_points discovery (v3 spec §5.4).

Adding a third-party plugin:
- Create a package that defines a ``Plugin`` subclass.
- Register via ``pyproject.toml`` under group ``agent_harness.plugins``.
- Harness.__init__() automatically picks it up via ``discover_entry_points()``.

N66 fix: entry_points discovery is called by Harness (``discover_entry_points()``)
on construction; this is the canonical auto-discovery path.
"""
from __future__ import annotations

import importlib.metadata as md
import logging
from typing import TYPE_CHECKING

from .base import Plugin

if TYPE_CHECKING:
    from tradingagents.agent_harness.harness import Harness

LOGGER = logging.getLogger(__name__)


class PluginRegistry:
    def __init__(self, harness: "Harness") -> None:
        self.harness = harness
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        if not plugin.name:
            raise ValueError("plugin must declare a non-empty name")
        if plugin.name in self._plugins:
            raise ValueError(f"plugin {plugin.name!r} already installed")
        self._plugins[plugin.name] = plugin
        plugin.install(self.harness)
        LOGGER.info("plugin installed: %s v%s", plugin.name, plugin.version)

    def list(self) -> list[str]:
        return sorted(self._plugins)

    def get(self, name: str) -> Plugin:
        if name not in self._plugins:
            raise KeyError(f"plugin {name!r} not installed; known: {sorted(self._plugins)}")
        return self._plugins[name]

    def discover_entry_points(self, group: str = "agent_harness.plugins") -> int:
        try:
            eps = md.entry_points(group=group)
        except Exception as e:
            LOGGER.warning("plugin entry_points discovery failed: %s", e)
            return 0
        loaded = 0
        for ep in eps:
            try:
                plugin_cls = ep.load()
                plugin = plugin_cls()
                self.register(plugin)
                loaded += 1
            except Exception as e:
                LOGGER.warning("entry_points load failed for %s: %s", ep.name, e)
        return loaded
