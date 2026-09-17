"""MCP server — P6 migration target (v3 spec §11.2).

The harness-owned MCP server auto-exposes every tool from the
ToolRegistry (read tools by default; write tools opt-in via
``MCP_EXPOSE_WRITE_TOOLS=1`` or auto-approved by default). Replaces the
server; the old server is kept for backwards compatibility.

Start with::

    python -m tradingagents.agent_harness.mcp.server

Or via the registered entry-point (configured in ``pyproject.toml``).
"""
from .server import build_mcp_server, main

__all__ = ["build_mcp_server", "main"]
