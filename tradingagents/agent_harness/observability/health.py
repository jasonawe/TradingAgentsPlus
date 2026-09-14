"""HealthChecker — feeds `` /api/harness/health `` endpoint (v3 spec §7.2 #8).

Returns a single dict summarising every subsystem: providers, tools,
agents, plugins, circuit breaker, audit + metrics + tracing stats.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from tradingagents.agent_harness.harness import Harness


class HealthChecker:
    async def check_all(self, harness: "Harness") -> dict:
        # Provider health: each provider's lightweight ``health()`` method.
        provider_status: dict[str, dict] = {}
        for name, provider in harness.data_registry.items():
            try:
                provider_status[name] = provider.health()
            except Exception as e:
                provider_status[name] = {"name": name, "status": "error", "error": str(e)}

        # Tool count + permission split
        tools = harness.list_tools()
        from tradingagents.agent_harness.tools import PermissionType
        tool_reads = sum(1 for t in tools if t.permission == PermissionType.READ)
        tool_writes = sum(1 for t in tools if t.permission == PermissionType.WRITE)

        # Circuit breaker state
        cb_state = harness.circuit_breaker.state if harness.circuit_breaker else "n/a"

        return {
            "ok": True,
            "providers": provider_status,
            "tools": {
                "total": len(tools),
                "read": tool_reads,
                "write": tool_writes,
            },
            "agents": harness.agent_registry.list(),
            "plugins": harness.plugin_registry.list() if harness.plugin_registry else [],
            "circuit_breaker": cb_state,
            "data_dir": str(harness.config.data_dir),
            "stage": "P3+P4+P5+P6+P7",
        }
