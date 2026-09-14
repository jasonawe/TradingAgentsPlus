"""NewsPlugin — news tool + sentiment (v3 spec §5.4 + N63 fix)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from tradingagents.agent_harness.plugins.base import Plugin

if TYPE_CHECKING:
    from tradingagents.agent_harness.harness import Harness


class NewsPlugin(Plugin):
    name = "news"
    version = "1.0.0"
    description = "Recent news headlines + sentiment scoring"

    def tools(self):
        from tradingagents.agent_harness.tools.registry import ToolRegistry
        from tradingagents.agent_harness.tools import install_builtin_tools
        reg = ToolRegistry()
        install_builtin_tools(reg)
        return [reg.get("get_news")]

    def agents(self):
        return []  # NewsAgent core impl already registered by Harness.

    def prompts(self) -> dict[str, str]:
        return {
            "news_summary": "你是舆情分析师,擅长总结新闻情绪 + 关键事件。",
        }

    def install(self, harness: "Harness") -> None:
        super().install(harness)
