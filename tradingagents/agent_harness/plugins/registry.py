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
    def __init__(self, harness: "Harness", *, lazy: bool = False) -> None:
        """
        Args:
            harness: owning harness instance.
            lazy: when True, ``discover_entry_points`` only *records* entry
                points metadata; ``Plugin`` instances are constructed and
                installed only on first ``get(name)``. Use this to defer
                expensive imports (and side effects in
                ``Plugin.install(harness)``) until the plugin is actually
                needed. Builtin plugins registered via ``register()`` keep
                their eager-install semantics.
        """
        self.harness = harness
        self._lazy = lazy
        self._plugins: dict[str, Plugin] = {}
        # ``_pending`` holds ``(group, value)`` tuples for entry points
        # discovered in lazy mode; ``EntryPoint`` objects themselves are not
        # retained because we re-resolve them via ``entry_points(group=...)``
        # on first ``get(name)`` to stay correct across new installs.
        self._pending: dict[str, tuple[str, str]] = {}

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
        if name in self._plugins:
            return self._plugins[name]
        # Lazy path: resolve entry point on first access.
        if self._lazy and name in self._pending:
            group, _value = self._pending.pop(name)
            try:
                eps = md.entry_points(group=group)
                ep = next((e for e in eps if e.name == name), None)
                if ep is None:
                    raise KeyError(
                        f"plugin {name!r} pending but no longer in entry_points"
                    )
                plugin = ep.load()()
            except Exception as e:
                raise KeyError(
                    f"lazy plugin {name!r} load failed: {e}"
                ) from e
            self.register(plugin)
            return self._plugins[name]
        raise KeyError(
            f"plugin {name!r} not installed; known: {sorted(self._plugins)}"
        )

    def pending(self) -> list[str]:
        """Names discovered but not yet loaded (lazy mode only)."""
        return sorted(self._pending)

    def is_lazy(self) -> bool:
        return self._lazy

    def discover_entry_points(self, group: str = "agent_harness.plugins") -> int:
        """Discover entry points.

        Lazy mode: only records ``(name -> (group, value))`` in ``_pending``;
        the actual import happens lazily in ``get(name)``. Returns the count
        discovered (not the count installed).

        Eager mode (default): imports and installs each plugin immediately,
        matching v3 spec §5.4 behavior. Returns the count successfully loaded.
        """
        try:
            eps = md.entry_points(group=group)
        except Exception as e:
            LOGGER.warning("plugin entry_points discovery failed: %s", e)
            return 0
        if self._lazy:
            for ep in eps:
                self._pending[ep.name] = (group, ep.value)
            return len(eps)
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
