"""§7.3 #7 — Lazy plugin load。

覆盖:
  - PluginRegistry(lazy=True) 默认不 import / install 任何 plugin
  - discover_entry_points 在 lazy 模式只记录 pending
  - get(name) 触发 lazy load
  - get 加载失败抛 KeyError
  - builtin register 仍即时 install
  - HarnessConfig.lazy_plugin_load 字段 + Harness wire
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


class _FakeHarness:
    """Minimal harness stand-in."""


def _make_fake_ep(name: str):
    """Build a fake EntryPoint-like object with .name/.value/.load().

    The returned ``cls()`` is a ``Plugin`` ABC-friendly shim with the right
    ``name`` attribute and no-op install/tools/agents/prompts so the registry
    can call them without exploding.
    """
    from tradingagents.agent_harness.plugins.base import Plugin

    class _Shim(Plugin):
        def __init__(self):
            self.installed_with = None

        def tools(self):
            return []

        def install(self, harness):
            self.installed_with = harness

    ep = MagicMock()
    ep.name = name
    ep.value = "fake.module:PluginCls"

    # Define a *real* Plugin subclass as the loaded class so the registry can
    # ``cls()`` it and call ``.name``/``.install`` without Mock attribute gotchas.
    class _Cls(_Shim):
        pass

    _Cls.name = name
    _Cls.version = "0.0.1"
    _Cls.__name__ = f"Plugin_{name}"

    def _load():
        return _Cls

    ep.load.side_effect = _load
    return ep
# ---------------------------------------------------------------------------
# PluginRegistry(lazy=True) 基本行为
# ---------------------------------------------------------------------------


def test_lazy_registry_discover_only_records():
    from tradingagents.agent_harness.plugins import PluginRegistry

    r = PluginRegistry(_FakeHarness(), lazy=True)
    eps = [_make_fake_ep("alpha"), _make_fake_ep("beta")]
    with patch("tradingagents.agent_harness.plugins.registry.md.entry_points", return_value=eps):
        n = r.discover_entry_points()
    assert n == 2
    assert r.list() == []
    assert r.pending() == ["alpha", "beta"]
    for ep in eps:
        ep.load.assert_not_called()


def test_eager_registry_loads_immediately():
    from tradingagents.agent_harness.plugins import PluginRegistry

    r = PluginRegistry(_FakeHarness(), lazy=False)
    ep = _make_fake_ep("alpha")
    with patch("tradingagents.agent_harness.plugins.registry.md.entry_points", return_value=[ep]):
        n = r.discover_entry_points()
    assert n == 1
    ep.load.assert_called_once()
    assert r.list() == ["alpha"]
    assert r.pending() == []


def test_lazy_get_triggers_install():
    from tradingagents.agent_harness.plugins import PluginRegistry

    r = PluginRegistry(_FakeHarness(), lazy=True)
    ep = _make_fake_ep("alpha")
    # Patch persists across discover + get.
    with patch("tradingagents.agent_harness.plugins.registry.md.entry_points", return_value=[ep]):
        r.discover_entry_points()
        plugin = r.get("alpha")
    assert plugin.name == "alpha"
    ep.load.assert_called_once()
    # 第二次 get 不再 load (cached)
    plugin2 = r.get("alpha")
    assert plugin2 is plugin
    ep.load.assert_called_once()


def test_lazy_get_load_failure_raises_keyerror():
    from tradingagents.agent_harness.plugins import PluginRegistry

    r = PluginRegistry(_FakeHarness(), lazy=True)
    ep = _make_fake_ep("alpha")
    ep.load.side_effect = ImportError("missing dep")
    with patch("tradingagents.agent_harness.plugins.registry.md.entry_points", return_value=[ep]):
        r.discover_entry_points()
        with pytest.raises(KeyError, match="lazy plugin 'alpha' load failed"):
            r.get("alpha")


def test_lazy_get_unknown_plugin_raises():
    from tradingagents.agent_harness.plugins import PluginRegistry

    r = PluginRegistry(_FakeHarness(), lazy=True)
    with pytest.raises(KeyError, match="not installed"):
        r.get("ghost")


def test_builtin_register_still_eager_in_lazy_mode():
    """builtin plugin 即使 lazy=True 也走 register → 立即 install。"""
    from tradingagents.agent_harness.plugins import PluginRegistry

    captured = []

    class _PluginShim:
        # bypass Plugin ABC so we don't need to implement tools/install
        name = "fake-builtin"
        version = "0.0.1"

        def install(self, harness):
            captured.append(harness)

    harness = _FakeHarness()
    r = PluginRegistry(harness, lazy=True)
    r.register(_PluginShim())
    assert r.list() == ["fake-builtin"]
    assert r.pending() == []
    assert captured == [harness]


# ---------------------------------------------------------------------------
# Harness wire — HarnessConfig.lazy_plugin_load
# ---------------------------------------------------------------------------


def test_harnessconfig_lazy_plugin_load_default_false():
    from tradingagents.agent_harness.config.schema import HarnessConfig
    cfg = HarnessConfig()
    assert cfg.lazy_plugin_load is False


def test_harness_wires_lazy_to_registry(tmp_path):
    from tradingagents.agent_harness.harness import Harness, HarnessConfig

    cfg = HarnessConfig(data_dir=tmp_path, lazy_plugin_load=True)
    h = Harness(cfg)
    assert h.plugin_registry.is_lazy() is True
    assert h.config.lazy_plugin_load is True


def test_harness_eager_default(tmp_path):
    from tradingagents.agent_harness.harness import Harness, HarnessConfig

    cfg = HarnessConfig(data_dir=tmp_path)
    h = Harness(cfg)
    assert h.plugin_registry.is_lazy() is False
