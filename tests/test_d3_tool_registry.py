"""ToolRegistry + builtin tool tests (v3 P3 / spec §5)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.tools import (  # noqa: E402
    BaseTool,
    FunctionTool,
    PermissionPolicy,
    PermissionType,
    ToolContext,
    ToolRegistry,
    install_builtin_tools,
)
from tradingagents.agent_harness.tools.permission import PermissionType as PT  # noqa: E402


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------


def test_permission_policy_requires_approval_by_default() -> None:
    p = PermissionPolicy()
    assert p.requires_approval("user:trade:real") is True


def test_permission_policy_auto_approve_prefix() -> None:
    p = PermissionPolicy(auto_approve_scopes=["dev", "test"])
    assert p.requires_approval("dev:foo") is False
    assert p.requires_approval("user:trade") is True


def test_permission_policy_explicit_require() -> None:
    p = PermissionPolicy(require_approval_scopes=["prod"])
    assert p.requires_approval("prod:trade") is True
    assert p.requires_approval("user:trade") is True


def test_permission_policy_annotate() -> None:
    p = PermissionPolicy(auto_approve_scopes=["dev"])
    res = p.annotate(["dev:foo", "user:bar"])
    assert res == {"dev:foo": False, "user:bar": True}


# ---------------------------------------------------------------------------
# Registry basics
# ---------------------------------------------------------------------------


def test_registry_rejects_duplicate() -> None:
    reg = ToolRegistry()

    @reg.register(
        name="dup_tool",
        description="x",
        args_schema=int,
        result_schema=int,
    )
    def t1(x: int) -> int:
        return x

    with pytest.raises(ValueError):

        @reg.register(
            name="dup_tool",
            description="x",
            args_schema=int,
            result_schema=int,
        )
        def t2(x: int) -> int:
            return x


def test_registry_get_and_list() -> None:
    reg = ToolRegistry()
    reg.add(_StubTool())
    assert reg.get("stub").name == "stub"
    assert "stub" in reg.list_names()


def test_registry_list_by_permission() -> None:
    reg = ToolRegistry()
    reg.add(_StubTool())  # read
    reg.add(_StubTool(name="wb", perm=PermissionType.WRITE))
    assert len(reg.list_by_permission(PermissionType.READ)) == 1
    assert len(reg.list_by_permission(PermissionType.WRITE)) == 1
    assert len(reg.list_by_permission(PermissionType.WORKFLOW)) == 0


def test_registry_function_tool_wraps_sync() -> None:
    reg = ToolRegistry()

    @reg.register(
        name="square",
        description="x²",
        args_schema=int,
        result_schema=int,
    )
    def square(args: int) -> int:
        return args * args

    import asyncio
    out = asyncio.run(reg.get("square").invoke(7, ToolContext(session_id="t")))
    assert out == 49


# ---------------------------------------------------------------------------
# Builtin tools
# ---------------------------------------------------------------------------


def test_install_builtin_tools_registers_17() -> None:
    reg = ToolRegistry()
    install_builtin_tools(reg)
    assert len(reg.list_all()) == 19


def test_builtin_tools_read_count() -> None:
    reg = ToolRegistry()
    install_builtin_tools(reg)
    reads = {t.name for t in reg.list_by_permission(PT.READ)}
    expected_reads = {
        "get_quote",
        "get_quotes_batch",
        "get_history",
        "get_fundamentals",
        "get_news",
        "list_alpha_factors",
        "compute_alpha_factors",
        "evaluate_alpha",
        "list_watchlist",
        "list_scheduled_tasks",
    }
    assert expected_reads.issubset(reads), f"missing: {expected_reads - reads}"


def test_builtin_tools_write_count() -> None:
    reg = ToolRegistry()
    install_builtin_tools(reg)
    writes = {t.name for t in reg.list_by_permission(PT.WRITE)}
    assert writes == {
        "create_alert",
        "update_alert",
        "delete_alert",
        "create_note",
        "update_note",
        "delete_note",
        "create_scheduled_task",
        "update_scheduled_task",
        "delete_scheduled_task",
    }


def test_create_alert_returns_pending_approval() -> None:
    reg = ToolRegistry()
    install_builtin_tools(reg)
    from tradingagents.agent_harness.tools.builtin import AlertWriteArgs
    import asyncio
    tool = reg.get("create_alert")
    res = asyncio.run(tool.invoke(AlertWriteArgs(symbol="600036.SS", threshold=10.0), ToolContext(session_id="t")))
    assert res["status"] == "pending_approval"


def test_list_alpha_factors_returns_nonempty() -> None:
    reg = ToolRegistry()
    install_builtin_tools(reg)
    import asyncio
    from tradingagents.agent_harness.tools.builtin import ListAlphaFactorsResult
    res = asyncio.run(reg.get("list_alpha_factors").invoke(None, ToolContext(session_id="t")))
    assert isinstance(res, ListAlphaFactorsResult)
    assert len(res.factors) >= 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _StubTool(BaseTool):
    schema = None  # type: ignore[assignment]

    def __init__(self, name: str = "stub", perm: PermissionType = PT.READ) -> None:
        from tradingagents.agent_harness.tools.schema import ToolSchema
        self.schema = ToolSchema(
            name=name,
            description="stub",
            args_schema=int,
            result_schema=int,
            permission=perm.value,
        )

    async def invoke(self, args, context: ToolContext) -> int:
        return 0
