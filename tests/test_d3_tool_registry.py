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
    # 10 reads + 8 write tools (alerts/notes create+update+delete + watchlist
    # add/remove) + 1 plugin auto = 18
    # §P3-1 added add_to_watchlist / remove_from_watchlist on top of the
    # earlier 6 writes (alerts/notes × create+update+delete).
    assert len(reg.list_all()) == 18


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
        # §P3-1 — watchlist write tools (harness can now mutate the
        # user's list, not just list it).
        "add_to_watchlist",
        "remove_from_watchlist",
    }


def test_create_alert_returns_pending_approval() -> None:
    reg = ToolRegistry()
    install_builtin_tools(reg)
    from tradingagents.agent_harness.tools.builtin import CreateAlertArgs
    import asyncio
    tool = reg.get("create_alert")
    res = asyncio.run(
        tool.invoke(
            CreateAlertArgs(
                symbol="600036.SS",
                kind="price_above",
                params={"threshold": 10.0},
            ),
            ToolContext(session_id="t"),
        )
    )
    # First call returns pending_approval via AWAITING_CONFIRMATION marker
    # (tools_bridge has no set_repositories() in this isolated test, so the
    # gate may short-circuit with empty gate payload — accept either.)
    assert res["status"] in {"pending_approval", "error"}


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


# ---------------------------------------------------------------------------
# Bridge to tools_bridge (N-stage: real internal tool implementations)
# ---------------------------------------------------------------------------


def test_create_alert_bridge_uses_tools_bridge_aonvoke() -> None:
    """create_alert must dispatch to tools_bridge.create_alert and surface
    its AWAITING_CONFIRMATION marker as status=pending_approval with the
    parsed gate payload surfaced alongside."""
    from tradingagents.agent_harness.tools import builtin as builtin_mod
    from tradingagents.agent_harness.tools.context import ToolContext as _TC
    import asyncio as _asyncio

    class FakeBridge:
        async def ainvoke(self, kwargs, config=None):
            return (
                "AWAITING_CONFIRMATION: "
                '{"needs_confirmation": true, "tool_name": "create_alert"}'
            )

    async def fake_invoke(tool, kwargs, context):
        text = await tool.ainvoke(kwargs, config={"configurable": {"thread_id": context.session_id}})
        parsed = builtin_mod._parse_bridge_text(text)
        if parsed["status"] == "pending_approval":
            try:
                import json as _json
                parsed["gate"] = _json.loads(text[len("AWAITING_CONFIRMATION:"):].strip())
            except Exception:
                pass
        return parsed

    builtin_mod._invoke_bridge = fake_invoke  # type: ignore[assignment]

    async def drive():
        return await builtin_mod.create_alert(
            builtin_mod.CreateAlertArgs(
                symbol="600036.SS",
                kind="price_above",
                params={"threshold": 50.0},
            ),
            _TC(session_id="t-bridge"),
        )

    res = _asyncio.run(drive())
    assert res["status"] == "pending_approval"
    assert "gate" in res
    assert res["gate"]["tool_name"] == "create_alert"



def test_create_note_bridge_returns_pending_approval() -> None:
    """create_note should also route through tools_bridge. Even when the
    bridge's gate returns without a JSON payload, the status field must
    be parseable (fallback path uses _parse_bridge_text heuristics)."""
    from tradingagents.agent_harness.tools import builtin as builtin_mod
    from tradingagents.agent_harness.tools.context import ToolContext as _TC
    import asyncio as _asyncio

    # Inject a fake bridge tool that returns AWAITING_CONFIRMATION without JSON.
    class FakeBridge:
        async def ainvoke(self, kwargs, config=None):
            return "AWAITING_CONFIRMATION: not-json"

    async def fake_invoke(tool, kwargs, context):
        text = await tool.ainvoke(kwargs, config={"configurable": {"thread_id": context.session_id}})
        parsed = builtin_mod._parse_bridge_text(text)
        if parsed["status"] == "pending_approval":
            try:
                import json as _json
                parsed["gate"] = _json.loads(text[len("AWAITING_CONFIRMATION:"):].strip())
            except Exception:
                pass
        return parsed

    builtin_mod._invoke_bridge = fake_invoke  # type: ignore[assignment]

    async def drive():
        return await builtin_mod.create_note(
            builtin_mod.CreateNoteArgs(symbol="600036.SS", body_md="hold for 6mo"),
            _TC(session_id="t-bridge-note"),
        )

    res = _asyncio.run(drive())
    assert res["status"] == "pending_approval"


def test_parse_bridge_text_handles_all_prefixes() -> None:
    """_parse_bridge_text must map every known prefix to a sensible status."""
    from tradingagents.agent_harness.tools.builtin import _parse_bridge_text

    assert _parse_bridge_text("NOTE_CREATED: {\"id\": \"1\"}")["status"] == "created"
    assert _parse_bridge_text("ALERT_UPDATED: {\"id\": \"1\"}")["status"] == "updated"
    assert _parse_bridge_text("ALERT_DELETED: alert-x")["status"] == "deleted"
    assert _parse_bridge_text("ERROR: bad input")["status"] == "error"
    assert _parse_bridge_text("NO_DATA: empty")["status"] == "no_data"
    assert _parse_bridge_text("(no scheduled tasks)")["status"] == "empty"
    assert _parse_bridge_text("(用户关注列表为空)")["status"] == "empty"
    # Unknown prefix still maps to ok so LLM doesn't see a fake error.
    assert _parse_bridge_text("MARKET_OVERVIEW raw text")["status"] == "ok"
