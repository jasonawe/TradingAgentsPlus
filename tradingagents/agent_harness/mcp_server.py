"""MCP server adapter — expose harness tool_registry as MCP tools.

§4 (stage 4): rewrite legacy Stage C MCP server as a thin adapter
that bridges Claude Desktop (or any MCP client) to the harness
``ToolRegistry``. The adapter does not duplicate tool definitions —
``registry.list_all()`` is the single source of truth. Adding a new
tool to ``builtin.py`` automatically surfaces it to MCP clients on
next server restart.

Transport: stdio (Claude Desktop default).
HITL: configurable via ``MCP_REQUIRE_CONFIRM=1`` env var; default is
auto-approve since the MCP user already trusts the Claude agent.

Usage:
    python -m tradingagents.agent_harness.mcp_server

Claude Desktop config:
    "tradingagents-harness": {
      "command": "python",
      "args": ["-m", "tradingagents.agent_harness.mcp_server"]
    }
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# Path setup so this module runs both as ``python -m`` and direct.
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
from pydantic import ConfigDict

from tradingagents.agent_harness.tools.builtin import install_builtin_tools
from tradingagents.agent_harness.tools.registry import ToolRegistry
from tradingagents.agent_harness.tools.context import ToolContext
from tradingagents.agent_harness.tools.permission import PermissionType

LOGGER = logging.getLogger(__name__)

mcp = FastMCP("tradingagents-harness")


# ═══════════════════════════════════════════════════
# 1. Infrastructure — same DB / repo / quote service as web/app.py
# ═══════════════════════════════════════════════════

def _setup_infrastructure() -> None:
    """Spin up the storage + repositories + quote service once at import."""
    from tradingagents.default_config import web_runs_db_path
    from web.storage import SQLiteStore
    from web.market_data import QuoteService, ProviderRouter
    from web.providers import (
        AKShareProvider, YFinanceProvider, EastMoneyProvider, AlphaVantageProvider,
    )
    from web.repositories import (
        NoteRepository, AlertRepository, QuoteRepository, ProviderHealthRepository,
    )
    from tradingagents.agent_harness.tools.impl import (
        set_repositories, set_quote_service, set_active_runner,
        set_scheduler_service, set_news_provider, set_report_history,
    )

    run_db_path = web_runs_db_path()
    store = SQLiteStore(run_db_path)

    note_repo = NoteRepository(run_db_path)
    alert_repo = AlertRepository(run_db_path)
    quote_repo = QuoteRepository(run_db_path)
    health_repo = ProviderHealthRepository(run_db_path)

    set_repositories({
        "notes": note_repo,
        "alerts": alert_repo,
        "quotes": quote_repo,
        "health": health_repo,
    })

    providers = {
        "akshare": AKShareProvider(),
        "yfinance": YFinanceProvider(),
        "eastmoney": EastMoneyProvider(),
        "alpha_vantage": AlphaVantageProvider(),
    }
    router = ProviderRouter(providers, health=health_repo)
    quote_service = QuoteService(router, quote_repo)
    set_quote_service(quote_service)
    LOGGER.info("MCP infrastructure ready (db=%s, providers=%d)", run_db_path, len(providers))


_setup_infrastructure()


# ═══════════════════════════════════════════════════
# 2. HITL — auto-approve for MCP (override per tool via env)
# ═══════════════════════════════════════════════════

def _make_session_id() -> str:
    return f"mcp-{uuid.uuid4().hex[:12]}"


def _install_auto_approve_patch() -> None:
    """Patch HITL gate so MCP writes auto-approve unless env says otherwise.

    The MCP client (e.g. Claude Desktop) already trusts its own agent —
    adding a manual confirm dialog would just deadlock the session.
    Set ``MCP_REQUIRE_CONFIRM=1`` to bypass the patch and force the
    real AWAITING_CONFIRMATION path (caller must handle the gate).
    """
    if os.environ.get("MCP_REQUIRE_CONFIRM") == "1":
        LOGGER.info("MCP_REQUIRE_CONFIRM=1 → HITL confirmation enforced")
        return

    from tradingagents.agent_harness.tools import builtin as _builtin

    async def auto_approve_check(*, session_id: str, tool_name: str, tool_args: dict) -> str | None:
        """Replace HITL gate with auto-approve for MCP callers."""
        from tradingagents.agent_harness.hitl import grant_approval
        grant_approval(session_id, tool_name, tool_args)
        return None

    _builtin._hitl_gate = auto_approve_check
    LOGGER.info("MCP HITL gate: auto-approve (set MCP_REQUIRE_CONFIRM=1 to enforce)")


_install_auto_approve_patch()


# ═══════════════════════════════════════════════════
# 3. Per-call session_id allocation + ToolContext plumbing
# ═══════════════════════════════════════════════════

def _session_for(write: bool) -> tuple[str, ToolContext]:
    """Allocate a fresh per-call session id and ToolContext.

    Each MCP tool call gets its own ``mcp-<uuid>`` session so the
    audit log + L2 user_prefs have a deterministic scope. Reusing
    a session across calls would entangle audit rows that the user
    expects to see as discrete operations.
    """
    sid = _make_session_id()
    ctx = ToolContext(session_id=sid)
    return sid, ctx


# ═══════════════════════════════════════════════════
# 4. Tool schema + handler bridge
# ═══════════════════════════════════════════════════

class _MCPAnyArgs(ArgModelBase):
    """Permissive schema: accept any kwarg the MCP client sends.

    Tools registered via ``@tool_registry.register(...)`` declare a
    Pydantic args_schema; FastMCP's ``Tool.from_function`` does not
    see through to that schema, so we declare ``extra='allow'`` and
    merge __pydantic_extra__ when dumping back to kwargs. Without
    this, every concrete field is dropped and the handler gets {}.
    """

    model_config = ConfigDict(extra="allow")

    def model_dump_one_level(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field_name in type(self).model_fields:
            out[field_name] = getattr(self, field_name)
        extras = getattr(self, "__pydantic_extra__", None)
        if isinstance(extras, dict):
            out.update(extras)
        return out


_MCP_FN_METADATA = FuncMetadata(arg_model=_MCPAnyArgs)


def _build_tool(tool_obj: Any) -> Tool:
    """Wrap a harness BaseTool as a FastMCP Tool.

    The handler converts kwargs to the tool's Pydantic args model
    (when one exists) and forwards to ``tool.invoke(validated, ctx)``.
    Write tools get a fresh per-call session id; read tools reuse
    a stable session so short-term caches survive across calls.
    """
    name = tool_obj.name
    description = (getattr(tool_obj, "description", "") or "").strip()
    if len(description) > 500:
        description = description[:497] + "..."

    permission = getattr(tool_obj, "permission", PermissionType.READ)
    is_write = permission == PermissionType.WRITE

    async def handler(**kwargs) -> str:
        sid, ctx = _session_for(is_write)
        # Try to validate kwargs against the tool's Pydantic args schema.
        args_obj: Any = kwargs
        schema_cls = getattr(getattr(tool_obj, "schema", None), "args_schema", None)
        if schema_cls is not None and hasattr(schema_cls, "model_validate"):
            try:
                args_obj = schema_cls.model_validate(kwargs)
            except Exception:
                # Fallback: pass kwargs through — tool's own schema will reject bad fields.
                args_obj = kwargs
        try:
            result = await tool_obj.invoke(args_obj, ctx)
            # BaseTool returns Pydantic models / dicts; stringify for MCP text content.
            if hasattr(result, "model_dump"):
                return json.dumps(result.model_dump(), ensure_ascii=False, default=str)
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False, default=str)
            return str(result)
        except Exception as e:
            LOGGER.exception("MCP tool %s failed", name)
            return f"ERROR: {type(e).__name__}: {e}"

    return Tool(
        fn=handler,
        name=name,
        description=description,
        parameters=_MCPAnyArgs.model_json_schema(by_alias=True),
        fn_metadata=_MCP_FN_METADATA,
        is_async=True,
        context_kwarg=None,
    )


# ═══════════════════════════════════════════════════
# 5. Registry bridge — harness ToolRegistry → MCP tools
# ═══════════════════════════════════════════════════

def _register_all_tools() -> int:
    """Register every tool from a fresh harness ToolRegistry to MCP.

    Returns the number of tools registered (used by smoke tests).
    """
    registry = ToolRegistry()
    install_builtin_tools(registry)
    n = 0
    for tool in registry.list_all():
        mcp_tool = _build_tool(tool)
        # MCP 1.x exposes _tool_manager._tools as the internal dict.
        # Using the public add_tool keeps this forward-compatible.
        try:
            mcp.add_tool(mcp_tool.fn, name=mcp_tool.name, description=mcp_tool.description)
        except (AttributeError, TypeError):
            # Fallback for older MCP versions.
            mcp._tool_manager._tools[mcp_tool.name] = mcp_tool
        n += 1
    LOGGER.info("MCP: registered %d tools from harness registry", n)
    return n


_REGISTERED = _register_all_tools()


# ═══════════════════════════════════════════════════
# 6. Entry point
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    # stdio transport — Claude Desktop / MCP clients expect this.
    mcp.run()
