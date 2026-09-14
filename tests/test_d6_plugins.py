"""P6 tests: Plugin system + 3 builtin plugins + entry_points discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.config.schema import HarnessConfig  # noqa: E402
from tradingagents.agent_harness.harness import Harness  # noqa: E402
from tradingagents.agent_harness.plugins import Plugin, PluginRegistry  # noqa: E402
from tradingagents.agent_harness.plugins.builtin import (  # noqa: E402
    AlertPlugin,
    NewsPlugin,
    QuantPlugin,
)


# ---------------------------------------------------------------------------
# PluginRegistry basics
# ---------------------------------------------------------------------------


def test_plugin_registry_installs_plugin() -> None:
    h = Harness(HarnessConfig.from_env())
    reg = PluginRegistry(h)

    class _DemoPlugin(Plugin):
        name = "demo"
        version = "0.0.1"
        description = "test"

        def tools(self):
            return []

        def agents(self):
            return []

        def install(self, harness):
            super().install(harness)

    p = _DemoPlugin()
    reg.register(p)
    assert "demo" in reg.list()


def test_plugin_registry_rejects_duplicate() -> None:
    h = Harness(HarnessConfig.from_env())
    reg = PluginRegistry(h)

    class _DemoPlugin(Plugin):
        name = "dup"
        version = "0.0.1"

        def tools(self):
            return []

        def agents(self):
            return []

        def install(self, harness):
            super().install(harness)

    reg.register(_DemoPlugin())
    with pytest.raises(ValueError):
        reg.register(_DemoPlugin())


def test_plugin_registry_rejects_empty_name() -> None:
    h = Harness(HarnessConfig.from_env())
    reg = PluginRegistry(h)

    class _NoName(Plugin):
        name = ""
        version = "0"

        def tools(self):
            return []

        def agents(self):
            return []

        def install(self, harness):
            super().install(harness)

    with pytest.raises(ValueError):
        reg.register(_NoName())


# ---------------------------------------------------------------------------
# Builtin plugins
# ---------------------------------------------------------------------------


def test_builtin_plugins_register_via_harness() -> None:
    h = Harness(HarnessConfig.from_env())
    plugins = h.plugin_registry.list()
    assert {"quant", "news", "alert"}.issubset(plugins)


def test_quant_plugin_provides_alpha_prompt() -> None:
    h = Harness(HarnessConfig.from_env())
    assert hasattr(h, "_prompt_alpha_explanation")
    assert "IC" in h._prompt_alpha_explanation


def test_news_plugin_provides_news_prompt() -> None:
    h = Harness(HarnessConfig.from_env())
    assert hasattr(h, "_prompt_news_summary")
    assert "舆情" in h._prompt_news_summary or "情绪" in h._prompt_news_summary


def test_alert_plugin_provides_alert_template() -> None:
    h = Harness(HarnessConfig.from_env())
    assert hasattr(h, "_prompt_alert_template")
    assert "{symbol}" in h._prompt_alert_template


def test_plugin_installation_is_idempotent_for_agents() -> None:
    """N63 fix: plugins return references; should not re-register agents."""
    h = Harness(HarnessConfig.from_env())
    initial = set(h.agent_registry.list())
    # Already-registered plugin raises ValueError; that's the public surface.
    with pytest.raises(ValueError):
        h.plugin_registry.register(QuantPlugin())
    # Idempotency check: agent count unchanged.
    assert set(h.agent_registry.list()) == initial


def test_plugin_installation_is_idempotent_for_tools() -> None:
    """QuantPlugin.tools() references builtin tools; should not double-register."""
    # Use a *fresh* harness + plugin registry to verify Plugin.install() never
    # doubles tools even when called twice.
    from tradingagents.agent_harness.plugins import PluginRegistry
    h = Harness(HarnessConfig.from_env())
    # Build a parallel registry + force-install via the underlying install()
    # method twice with the same plugin (simulating re-install).
    p = QuantPlugin()
    n_before = len(h.tool_registry.list_names())
    p.install(h)
    p.install(h)  # second install must not raise
    assert len(h.tool_registry.list_names()) == n_before


# ---------------------------------------------------------------------------
# Harness integration — entry_points discovery
# ---------------------------------------------------------------------------


def test_harness_discovers_entry_points_without_crash(monkeypatch) -> None:
    # discover_entry_points with no third-party plugins → returns 0 silently.
    h = Harness(HarnessConfig.from_env())
    assert h.plugin_registry.list()  # at least 3 builtin
