"""MCP server — auto-exposes ToolRegistry (v3 spec §11.2).

Replaces the old hardcoded 15-tool ``tradingagents/agents/general/mcp_server.py``
by walking ``ToolRegistry.list_all()`` and registering every read tool with
FastMCP. Write tools are gated by ``MCP_EXPOSE_WRITE_TOOLS`` (default off —
HITL still enforced upstream via PermissionType.WRITE).

Environment variables
---------------------
``MCP_EXPOSE_WRITE_TOOLS`` (default ``0``): set to ``1`` to also expose
    write tools (auto-approved inside the MCP context).
``MCP_REQUIRE_CONFIRM`` (default ``0``): when ``1``, write tools return
    ``AWAITING_CONFIRMATION`` instead of auto-approving.
``MCP_TRANSPORT`` (default ``stdio``): ``stdio`` or ``sse``.

Standalone invocation::

    python -m tradingagents.agent_harness.mcp.server

Programmatic use::

    from tradingagents.agent_harness.mcp import build_mcp_server
    mcp = build_mcp_server()
    mcp.run(transport="stdio``)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) == "1"


def _expose_write_tools() -> bool:
    return _env_flag("MCP_EXPOSE_WRITE_TOOLS")


def _require_confirm() -> bool:
    return _env_flag("MCP_REQUIRE_CONFIRM")


def _executable(tool) -> Callable[..., Any]:
    """Convert a BaseTool into a sync-or-async function FastMCP can register."""
    import inspect
    import asyncio

    if inspect.iscoroutinefunction(tool.invoke):
        async def _async_invoker(*args: Any, **kwargs: Any) -> Any:
            return await tool.invoke(*args, **kwargs)
        return _async_invoker

    def _sync_invoker(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(tool.invoke(*args, **kwargs))
    return _sync_invoker


def _description(tool) -> str:
    return getattr(tool, "description", "") or tool.schema.description


def build_mcp_server():
    """Build a FastMCP server wired to ``ToolRegistry``.

    Pulls tools from the global ``ToolRegistry`` (P3 install_builtin_tools
    fills 19 tools; plugin ``tools()`` adds more). Returns a fresh
    FastMCP instance; the caller chooses transport + run.
    """
    from mcp.server.fastmcp import FastMCP

    from tradingagents.agent_harness.tools import (
        PermissionType,
        ToolRegistry,
        install_builtin_tools,
    )

    # Build a fresh registry + expose every installed tool.
    registry = ToolRegistry()
    install_builtin_tools(registry)

    mcp = FastMCP(
        name="tradingagents-agent",
        instructions=(
            "TradingAgents 理财通用 Agent — tools 自动从 ToolRegistry 暴露。"
            "Layer-1 read tools 默认暴露;Layer-2 write tools 需要 "
            "MCP_EXPOSE_WRITE_TOOLS=1。"
        ),
    )

    exposed = 0
    skipped_write = 0
    for tool in registry.list_all():
        perm = tool.permission
        if perm == PermissionType.WRITE and not _expose_write_tools():
            skipped_write += 1
            continue
        try:
            mcp.add_tool(
                _executable(tool),
                name=tool.name,
                description=_description(tool),
            )
            exposed += 1
        except Exception as e:
            LOGGER.warning("failed to expose tool %s: %s", tool.name, e)

    LOGGER.info(
        "MCP server ready: exposed=%d, skipped_write=%d, expose_writes=%s",
        exposed,
        skipped_write,
        _expose_write_tools(),
    )
    return mcp


def main() -> None:
    """Standalone entry point — ``python -m tradingagents.agent_harness.mcp.server``."""
    logging.basicConfig(level=os.environ.get("MCP_LOG_LEVEL", "INFO"))
    mcp = build_mcp_server()
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":  # pragma: no cover
    main()
