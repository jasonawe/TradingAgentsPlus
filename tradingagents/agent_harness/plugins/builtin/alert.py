"""AlertPlugin — write tools (alert/note/scheduled) + HITL policy (v3 spec §5.4)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from tradingagents.agent_harness.plugins.base import Plugin

if TYPE_CHECKING:
    from tradingagents.agent_harness.harness import Harness


class AlertPlugin(Plugin):
    name = "alert"
    version = "1.0.0"
    description = "HITL-gated write tools for alerts / notes / scheduled tasks"

    def tools(self):
        return []

    def agents(self):
        return []

    def prompts(self) -> dict[str, str]:
        return {
            "alert_template": (
                "[{exchange}] {asset_name}({symbol}) {change_pct:+.2f}%  "
                "现价 {price} (基准 {threshold})\n"
                "触发时间: {trigger_time}"
            ),
        }

    def install(self, harness: "Harness") -> None:
        super().install(harness)
