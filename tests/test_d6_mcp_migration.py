"""MCP server migration tests (P6 §11.2 — auto-expose ToolRegistry).

Validates that the new harness-owned MCP server walks ToolRegistry and
exposes tools according to ``MCP_EXPOSE_WRITE_TOOLS``. Skipped when
``mcp`` is not installed in the test environment.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

pytest.importorskip("mcp.server.fastmcp")
mcp_server = importlib.import_module("tradingagents.agent_harness.mcp")


def _collect_tool_names(mcp) -> list[str]:
    if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
        return list(mcp._tool_manager._tools.keys())
    if hasattr(mcp, "_tools"):
        return list(mcp._tools.keys())
    return []


def test_default_exposes_only_read_tools(monkeypatch) -> None:
    monkeypatch.delenv("MCP_EXPOSE_WRITE_TOOLS", raising=False)
    # Reload module so the env is read freshly.
    importlib.reload(mcp_server)
    mcp = mcp_server.build_mcp_server()
    names = set(_collect_tool_names(mcp))
    expected_read = {
        "get_quote", "get_quotes_batch", "get_history", "get_fundamentals",
        "get_news", "list_alpha_factors", "compute_alpha_factors",
        "evaluate_alpha", "list_watchlist", "list_scheduled_tasks",
    }
    assert expected_read.issubset(names), f"missing reads: {expected_read - names}"
    assert not any(n.startswith("create_") for n in names)
    assert not any(n.startswith("update_") for n in names)
    assert not any(n.startswith("delete_") for n in names)


def test_env_exposes_write_tools_when_set(monkeypatch) -> None:
    monkeypatch.setenv("MCP_EXPOSE_WRITE_TOOLS", "1")
    importlib.reload(mcp_server)
    mcp = mcp_server.build_mcp_server()
    names = set(_collect_tool_names(mcp))
    expected_writes = {
        "create_alert", "update_alert", "delete_alert",
        "create_note", "update_note", "delete_note",
        "create_scheduled_task", "update_scheduled_task", "delete_scheduled_task",
    }
    assert expected_writes.issubset(names), f"missing writes: {expected_writes - names}"


def test_old_mcp_server_still_importable() -> None:
    """Backward compatibility — old path keeps working (spec §11.2)."""
    old = importlib.import_module("tradingagents.agents.general.mcp_server")
    assert old is not None


def test_mcp_server_name_is_stable() -> None:
    importlib.reload(mcp_server)
    mcp = mcp_server.build_mcp_server()
    assert mcp.name == "tradingagents-agent"
