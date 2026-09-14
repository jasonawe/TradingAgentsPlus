"""QuantPlugin — alpha158 factor tools + prompt (v3 spec §5.4 + N63 fix)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from tradingagents.agent_harness.plugins.base import Plugin

if TYPE_CHECKING:
    from tradingagents.agent_harness.harness import Harness


class QuantPlugin(Plugin):
    name = "quant"
    version = "1.0.0"
    description = "Alpha158 factor computation + IC/Rank IC evaluation"

    def tools(self):
        # Alpha158 tool instances — reused from builtin registry.
        from tradingagents.agent_harness.tools.registry import ToolRegistry
        from tradingagents.agent_harness.tools import install_builtin_tools
        reg = ToolRegistry()
        install_builtin_tools(reg)
        return [reg.get(n) for n in (
            "list_alpha_factors", "compute_alpha_factors", "evaluate_alpha",
        )]

    def agents(self):
        return []  # AlphaAgent core impl already registered by Harness.

    def prompts(self) -> dict[str, str]:
        return {
            "alpha_explanation": (
                "你是量化研究员,擅长解释 alpha 因子的 IC/IR, "
                "用通俗语言总结 Rank IC 的预测能力。"
            ),
        }

    def install(self, harness: "Harness") -> None:
        super().install(harness)
