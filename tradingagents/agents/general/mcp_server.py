"""Stage C — MCP server,expose 15 tools 给 Claude Desktop 等 MCP 客户端。

设计:
- stdio transport(默认 — 适合本地 Claude Desktop 集成)
- 用 FastMCP(mcp.server.fastmcp)高层 API
- 15 个 tool 全部暴露:get_quote/get_quotes_batch/get_history/get_fundamentals/
  list_watchlist + 3 个 alpha + 7 个写工具
- session_id 来源:
  - 写工具:如果 MCP 调用没传,自动生成 "mcp_<uuid>" 作为 session_id
  - 注入到 config({"configurable": {"thread_id": session_id}})
- HITL 处理:
  - 默认 auto-approve(MCP 用户已经信任 Claude agent)
  - 设 MCP_REQUIRE_CONFIRM=1 走严格 HITL,返回 AWAITING_CONFIRMATION
    让调用方用 LangGraph 的 interrupt/Command 处理

启动:
    python -m tradingagents.agents.general.mcp_server
或:
    python tradingagents/agents/general/mcp_server.py

Claude Desktop 配置示例:
    "tradingagents-agent": {
      "command": "python",
      "args": ["-m", "tradingagents.agents.general.mcp_server"],
      "env": {"MINIMAX_CN_API_KEY": "..."}
    }
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# 路径修正 — 让 mcp_server 能独立运行
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
from pydantic import ConfigDict

# 导入 ALL_TOOLS + 注入 helpers
from tradingagents.agents.general.tools_bridge import (
    ALL_TOOLS,
    set_repositories,
    set_quote_service,
)

# 构造基础设施(同 web/app.py 的最小子集)
from tradingagents.default_config import DEFAULT_CONFIG
from web.storage import SQLiteStore
from web.market_data import QuoteService, ProviderRouter
from web.providers import (
    AKShareProvider, YFinanceProvider, EastMoneyProvider, AlphaVantageProvider,
)
from web.repositories import (
    NoteRepository, AlertRepository, QuoteRepository, ProviderHealthRepository,
)


# ════════════════════════════════════════════════════════
# 1. 构造 Store + Repositories + QuoteService(同 web/app.py)
# ════════════════════════════════════════════════════════

def _setup_infrastructure() -> None:
    """构造 MCP server 需要的 db store + repos + quote service。"""
    results_dir = DEFAULT_CONFIG.get("results_dir") or "."
    run_db_path = (
        DEFAULT_CONFIG.get("web_runs_db")
        or (Path(results_dir) / "web_runs.sqlite3")
    )
    # 转成 ~/.tradingagents/web_runs.sqlite3(对齐 web/app.py 默认行为)
    if not run_db_path.is_absolute():
        run_db_path = Path.home() / ".tradingagents" / "web_runs.sqlite3"

    store = SQLiteStore(run_db_path)

    # repos
    note_repo = NoteRepository(store)
    alert_repo = AlertRepository(store)
    quote_repo = QuoteRepository(store)
    provider_health_repo = ProviderHealthRepository(store)

    set_repositories({"notes": note_repo, "alerts": alert_repo})

    # QuoteService
    providers = {
        "yfinance": YFinanceProvider(),
        "alpha_vantage": AlphaVantageProvider(),
        "eastmoney": EastMoneyProvider(),
        "akshare": AKShareProvider(),
    }
    router = ProviderRouter(providers, health=provider_health_repo)
    quote_service = QuoteService(
        router, quote_repo, settings=None, config=DEFAULT_CONFIG,
    )
    set_quote_service(quote_service)


_setup_infrastructure()


# ════════════════════════════════════════════════════════
# 2. FastMCP server
# ════════════════════════════════════════════════════════

mcp = FastMCP(
    name="tradingagents-agent",
    instructions=(
        "TradingAgents 理财通用 Agent — 15 个 tool 可用:\n"
        "  读 (5): get_quote / get_quotes_batch / get_history / "
        "get_fundamentals / list_watchlist\n"
        "  Alpha (3): list_alpha_factors / compute_alpha_factors / evaluate_alpha\n"
        "  写 (7): create_note / update_note / delete_note / "
        "create_alert / update_alert / delete_alert / update_preference\n"
        "写工具会触发 audit log + auto-approve(MCP context)。"
    ),
)

# HITL 模式
_REQUIRE_CONFIRM = os.environ.get("MCP_REQUIRE_CONFIRM", "0") == "1"


def _make_session_id() -> str:
    return f"mcp_{uuid.uuid4().hex[:16]}"


def _auto_approve_session(session_id: str) -> None:
    """MCP context: 预 grant 所有写 tool 的 approval,让它们直接执行。

    注意:这是把 grant_approval 的全部组合预填进去,实现"全部批准"。
    """
    from tradingagents.agents.general.approval import grant_approval
    # 写工具的所有可能 args 组合不在这里枚举 — approval 是按 (tool_name, args) 注册的
    # 所以更简单的做法是 monkey-patch _check_write_approval
    # 但保持最小侵入:在工具调用前 grant 一个万能 sentinel
    # 实际做不到 — 只能等调用时 grant
    # 所以改方案:把所有 7 个写工具的 approval check 旁路掉
    pass


# Monkey-patch _check_write_approval 来 MCP 模式下 auto-approve
_ORIGINAL_CHECK = None


def _install_auto_approve_patch() -> None:
    """替换 _check_write_approval:在 MCP context 下永远返回 None(放行)。"""
    global _ORIGINAL_CHECK
    from tradingagents.agents.general import tools_bridge

    if _REQUIRE_CONFIRM:
        return  # 严格模式,不 patch

    _ORIGINAL_CHECK = tools_bridge._check_write_approval

    def auto_approve_check(*, session_id: str, tool_name: str, tool_args: dict) -> str | None:
        """MCP 模式:写工具直接放行,但仍记录 audit log。"""
        # 仍调用原 check 来写 audit log
        result = _ORIGINAL_CHECK(
            session_id=session_id, tool_name=tool_name, tool_args=tool_args,
        )
        # 如果是 AWAITING_CONFIRMATION,改成"已自动批准 + 标记"
        if isinstance(result, str) and result.startswith("AWAITING_CONFIRMATION:"):
            try:
                payload = json.loads(result.split(":", 1)[1].strip())
                audit_id = payload.get("audit_id")
                if audit_id:
                    from tradingagents.agents.general.audit import update_write_status
                    try:
                        update_write_status(
                            None, audit_id,
                            status="confirmed", confirmed_by="mcp-auto",
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            # grant approval 让 tool 真正执行
            from tradingagents.agents.general.approval import grant_approval
            grant_approval(session_id, tool_name, tool_args)
            return None  # 放行
        return result

    tools_bridge._check_write_approval = auto_approve_check


_install_auto_approve_patch()

# ════════════════════════════════════════════════════════
# 3. 宽松 Pydantic model:接收任何 kwargs(per-tool handler 走 **kwargs)
# ════════════════════════════════════════════════════════

class _MCPAnyArgs(ArgModelBase):
    """15 个 LangChain tool 共享的宽松 args schema。

    FastMCP 在 Tool.run() 里会用 arg_model.model_validate(arguments) 校验客户端
    发来的字段,如果字段在 schema 里没声明就会报 "Field required" — 这是原来
    **kwargs handler 失败的根因。改成 extra='allow' 后任何字段都接受,
    然后通过重写 model_dump_one_level() 把 extra 字段也 dump 出去(handler 通过 **kwargs
    拿到所有字段)。
    """

    model_config = ConfigDict(extra="allow")

    def model_dump_one_level(self) -> dict[str, Any]:
        """Override: 同时 dump declared fields + Pydantic 2 的 __pydantic_extra__。

        默认 ArgModelBase.model_dump_one_level 只 dump 声明字段,会把 extras 丢光,
        导致下游 handler 收到空 kwargs。Extras 在 Pydantic 2 里存在 __pydantic_extra__
        (一个 dict),显式 merge 进来。
        """
        out: dict[str, Any] = {}
        for field_name in type(self).model_fields:
            out[field_name] = getattr(self, field_name)
        extras = getattr(self, "__pydantic_extra__", None)
        if isinstance(extras, dict):
            out.update(extras)
        return out


_MCP_FN_METADATA = FuncMetadata(arg_model=_MCPAnyArgs)


def _build_tool(tool_obj: Any) -> Tool:
    """手动构造 FastMCP Tool,绕过 inspect.signature 推断。

    FastMCP 的 ``add_tool`` 走 ``Tool.from_function(fn)`` → ``func_metadata(fn)``,
    对 ``async def handler(**kwargs)`` 推断出空 schema → 客户端发任何参数都校验失败。
    这里直接构造 Tool,显式给宽松 schema 和 ``is_async=True``。
    """
    name = tool_obj.name
    description = (tool_obj.description or "").strip()
    if len(description) > 500:
        description = description[:497] + "..."

    async def handler(**kwargs) -> str:
        is_write = name.startswith(("create_", "delete_", "update_"))
        # LangChain tool.invoke(input, config=None, **kwargs):
        #   config 作为独立参数传入,内部 _prep_run_args 会保证它进
        #   tool.run() 的 run_kwargs,然后 _get_runnable_config_param
        #   找到函数签名里 InjectedToolArg 标注的 config 参数注入。
        # 不能塞到 kwargs 里,否则会被 _to_args_and_kwargs 按 self.args
        # 过滤掉(只留 declared 字段)。
        run_config = None
        if is_write:
            session_id = _make_session_id()
            run_config = {"configurable": {"thread_id": session_id}}

        try:
            result = tool_obj.invoke(kwargs, config=run_config)
            return str(result)
        except Exception as e:
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


# ════════════════════════════════════════════════════════
# 4. 注册 15 个 tool(见下方)
# ════════════════════════════════════════════════════════

# 注册所有 tool 到 MCP server — 手动构造 Tool 绕过 inspect.signature 推断
# 直接塞进 mcp._tool_manager._tools(MCP 1.30.0 内部 dict,稳定接口)
for _tool in ALL_TOOLS:
    _mcp_tool = _build_tool(_tool)
    mcp._tool_manager._tools[_mcp_tool.name] = _mcp_tool


# ════════════════════════════════════════════════════════
# 5. 入口
# ════════════════════════════════════════════════════════

if __name__ == "__main__":
    # stdio transport(默认 — Claude Desktop / MCP 客户端期望这个)
    mcp.run()
