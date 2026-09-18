"""Step 23 — verify get_quote + add_to_watchlist carry metadata.

Two migrated sample tools demonstrate the new metadata contract:
- ``get_quote`` is tagged QUOTE with display_view=quote
- ``add_to_watchlist`` is tagged WATCHLIST with display_view=ack

Other tools still work (no regression) but don't carry metadata yet.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_get_quote_has_metadata():
    from tradingagents.agent_harness.tools import ToolRegistry
    from tradingagents.agent_harness.tools.capabilities import Capability
    reg = ToolRegistry()
    from tradingagents.agent_harness.tools.builtin import install_builtin_tools
    install_builtin_tools(reg)
    schema = reg.get("get_quote").schema
    assert "capabilities" in schema.metadata
    assert Capability.QUOTE.value in schema.metadata["capabilities"]
    assert schema.metadata["display_view"] == "quote"
    assert schema.metadata["category"] == "data"


def test_add_to_watchlist_has_metadata():
    from tradingagents.agent_harness.tools import ToolRegistry
    from tradingagents.agent_harness.tools.capabilities import Capability
    reg = ToolRegistry()
    from tradingagents.agent_harness.tools.builtin import install_builtin_tools
    install_builtin_tools(reg)
    schema = reg.get("add_to_watchlist").schema
    assert Capability.WATCHLIST.value in schema.metadata["capabilities"]
    assert schema.metadata["display_view"] == "ack"
    assert schema.metadata["category"] == "crud"


def test_every_builtin_tool_has_metadata_after_step_25():
    """After Step 24 + Step 25 every tool in the builtin registry
    carries D2 metadata. The harness can safely call
    ``schema.metadata['capabilities']`` for any registered tool.
    """
    from tradingagents.agent_harness.tools import ToolRegistry
    reg = ToolRegistry()
    from tradingagents.agent_harness.tools.builtin import install_builtin_tools
    install_builtin_tools(reg)
    empty = [
        n for n in reg.list_names()
        if not reg.get(n).schema.metadata
    ]
    assert not empty, f"unmigrated tools still exist: {empty}"


def test_lifecycle_tracker_records_real_tool_call():
    """Wire FunctionTool.invoke → LifecycleTracker and verify pre/post."""
    import asyncio
    from tradingagents.agent_harness.tools import ToolRegistry, ToolContext
    from tradingagents.agent_harness.tools.builtin import install_builtin_tools
    from tradingagents.agent_harness.tools.lifecycle import LifecycleTracker

    reg = ToolRegistry()
    install_builtin_tools(reg)
    tracker = LifecycleTracker()
    # monkeypatch the global tracker
    import tradingagents.agent_harness.tools.lifecycle as lt
    saved = lt._global_tracker
    lt._global_tracker = tracker
    try:
        tool = reg.get("get_quote")
        # Just check the lifecycle pre fires — we don't care about
        # the actual upstream call. Pass a bad symbol that errors.
        async def _run():
            try:
                await tool.invoke(
                    {"symbol": "INVALID_SYM"},
                    ToolContext(session_id="t"),
                )
            except Exception:
                pass
        asyncio.run(_run())
        phases = [e.phase for e in tracker.events if e.tool == "get_quote"]
        assert "pre" in phases
        assert ("post" in phases) or ("error" in phases)
    finally:
        lt._global_tracker = saved
