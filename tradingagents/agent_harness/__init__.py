"""tradingagents.agent_harness — 独立 harness 子包 (Day 11e v2 spec)。

Harness 是 agent 编排核心 + tool 适配 + context 注入 + verification 体系的统一入口。
v2 spec: docs/superpowers/specs/2026-09-12-agent-harness-modularization.md

公共 API:
    from tradingagents.agent_harness import Harness, HarnessConfig
"""

from tradingagents.agent_harness.harness import Harness
from tradingagents.agent_harness.config.schema import HarnessConfig

__version__ = "0.1.0"  # 跟随 P1 增量
__all__ = ["Harness", "HarnessConfig"]
