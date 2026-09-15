"""Q5 / P2-8 — Harness 集成 SystemPromptWaterfall。"""
from __future__ import annotations

import pytest


def test_harness_holds_waterfall(tmp_path):
    from tradingagents.agent_harness.harness import Harness, HarnessConfig
    from tradingagents.agent_harness.core.system_prompt_waterfall import (
        SystemPromptWaterfall, WaterfallSection,
    )
    h = Harness(HarnessConfig(data_dir=tmp_path))
    assert isinstance(h.system_prompt_waterfall, SystemPromptWaterfall)


def test_plugin_install_registers_section(tmp_path):
    """模拟 plugin 在 install() 中注册 WaterfallSection。"""
    from tradingagents.agent_harness.harness import Harness, HarnessConfig
    from tradingagents.agent_harness.core.system_prompt_waterfall import WaterfallSection

    h = Harness(HarnessConfig(data_dir=tmp_path))

    def fake_install(harness):
        harness.system_prompt_waterfall.add(WaterfallSection(
            name="plugin:quant",
            content="prefer EPS analysis",
            priority=80,
            source="plugin:quant",
        ))

    fake_install(h)

    out = h.system_prompt_waterfall.build({})
    assert "## plugin:quant (source=plugin:quant, priority=80)" in out
    assert "prefer EPS analysis" in out


def test_callable_section_sees_context(tmp_path):
    from tradingagents.agent_harness.harness import Harness, HarnessConfig
    from tradingagents.agent_harness.core.system_prompt_waterfall import WaterfallSection

    h = Harness(HarnessConfig(data_dir=tmp_path))
    h.system_prompt_waterfall.add(WaterfallSection(
        name="dynamic",
        content=lambda ctx: f"current_user={ctx['user']}",
        priority=70,
    ))
    out = h.system_prompt_waterfall.build({"user": "shen"})
    assert "current_user=shen" in out
