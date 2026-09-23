"""§0.4.27 — ``_intent_for_tool`` prefers ``tool.schema.metadata['display_view']``
over the legacy hardcoded ``_TOOL_INTENT_MAP`` fallback.

This lets a tool author wire a new friendly card without touching
ShortCircuit. Register a tool with ``metadata={'display_view':
'quote'}`` and ``display_html`` on its tool_result will pick the
``quote`` renderer automatically.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core.short_circuit import ShortCircuit
from tradingagents.agent_harness.tools.base import BaseTool
from tradingagents.agent_harness.tools.permission import PermissionType
from tradingagents.agent_harness.tools.schema import ToolSchema
from tradingagents.agent_harness.tools.registry import ToolRegistry


# ─────────────────────────────────────────────────────────────────
# Fake tools with controlled metadata
# ─────────────────────────────────────────────────────────────────


class TinyArgs(BaseModel):
    symbol: str


class TinyResult(BaseModel):
    symbol: str
    value: float = 1.0


class FakeTool(BaseTool):
    def __init__(self, schema: ToolSchema):
        self.schema = schema

    @property
    def name(self) -> str:
        return self.schema.name

    async def invoke(self, args, context):
        return TinyResult(symbol=args.symbol, value=1.0)


def _register(reg: ToolRegistry, name: str, *, display_view=None, capabilities=None) -> FakeTool:
    metadata = {}
    if display_view is not None:
        metadata["display_view"] = display_view
    if capabilities is not None:
        metadata["capabilities"] = capabilities
    schema = ToolSchema(
        name=name,
        description=f"fake {name}",
        args_schema=TinyArgs,
        result_schema=TinyResult,
        permission=PermissionType.READ.value,
        metadata=metadata,
    )
    tool = FakeTool(schema)
    reg.add(tool)
    return tool


# ─────────────────────────────────────────────────────────────────
# Core contract: metadata drives intent
# ─────────────────────────────────────────────────────────────────


def test_metadata_display_view_string_drives_intent():
    """A string ``metadata['display_view']`` is the source of truth."""
    reg = ToolRegistry()
    _register(reg, "custom_viewer_tool", display_view="quote")
    sc = ShortCircuit(registry=reg)
    assert sc._intent_for_tool("custom_viewer_tool") == "quote"


def test_metadata_capabilities_list_falls_back_to_first_entry():
    """When ``display_view`` is absent, ``capabilities`` list is consulted.

    Some tools only declare ``capabilities`` (a list of tags). Take
    the first entry as the primary intent.
    """
    reg = ToolRegistry()
    _register(reg, "alpha_tagged_tool", capabilities=["alpha", "fundamentals", "quote"])
    sc = ShortCircuit(registry=reg)
    assert sc._intent_for_tool("alpha_tagged_tool") == "alpha"


def test_metadata_display_view_list_falls_back_to_first_entry():
    """Same shape as capabilities: a list with display_view key."""
    reg = ToolRegistry()
    _register(reg, "multi_cap_tool", display_view=["history", "quote"])
    sc = ShortCircuit(registry=reg)
    assert sc._intent_for_tool("multi_cap_tool") == "history"


def test_metadata_preferred_over_legacy_hardcoded_map():
    """If a tool is in BOTH the metadata bag AND the legacy
    ``_TOOL_INTENT_MAP``, the metadata wins."""
    reg = ToolRegistry()
    # ``create_note`` is in legacy _TOOL_INTENT_MAP as ``ack``.
    # Register it with metadata that says ``note_list`` instead.
    schema = ToolSchema(
        name="create_note",
        description="fake",
        args_schema=TinyArgs,
        result_schema=TinyResult,
        permission=PermissionType.WRITE.value,
        metadata={"display_view": "note_list", "side_effect_mode": "local_transactional"},
    )
    FakeTool(schema)
    reg.add(FakeTool(schema))
    sc = ShortCircuit(registry=reg)
    # Metadata wins — not the legacy "ack" mapping.
    assert sc._intent_for_tool("create_note") == "note_list"


def test_unknown_tool_returns_empty_string_for_friendly_card_skip():
    """A tool not in metadata AND not in legacy map → ``""`` so the
    friendly-card helper skips attaching ``display_html``."""
    reg = ToolRegistry()
    _register(reg, "totally_unknown_tool", display_view=None)
    sc = ShortCircuit(registry=reg)
    assert sc._intent_for_tool("totally_unknown_tool") == ""


def test_legacy_only_tool_still_resolves_to_ack():
    """Legacy tool (no metadata) that's in the hardcoded map resolves
    correctly via the fallback path.
    """
    reg = ToolRegistry()
    # add_watchlist is in the legacy map; register it WITHOUT metadata.
    schema = ToolSchema(
        name="add_watchlist",
        description="fake",
        args_schema=TinyArgs,
        result_schema=TinyResult,
        permission=PermissionType.WRITE.value,
        metadata={"side_effect_mode": "local_transactional"},
    )
    reg.add(FakeTool(schema))
    sc = ShortCircuit(registry=reg)
    assert sc._intent_for_tool("add_watchlist") == "ack"


def test_nonexistent_tool_returns_empty_string():
    """A tool that isn't registered at all should not crash."""
    reg = ToolRegistry()
    sc = ShortCircuit(registry=reg)
    assert sc._intent_for_tool("completely_made_up_tool") == ""


# ─────────────────────────────────────────────────────────────────
# End-to-end: _attach_display_html now uses metadata + legacy
# ─────────────────────────────────────────────────────────────────


def test_attach_display_html_uses_metadata_for_unmapped_tool():
    """A new tool registered with ``metadata={'display_view': 'quote'}``
    triggers the quote renderer. The legacy _TOOL_INTENT_MAP doesn't
    need to be updated."""
    reg = ToolRegistry()
    _register(reg, "custom_viewer_tool", display_view="quote")
    sc = ShortCircuit(registry=reg)
    out = sc._attach_display_html(
        "custom_viewer_tool",
        {
            "symbol": "AAPL",
            "price": 338.98,
            "change": 0.47,
            "change_pct": 1.16,
            "currency": "USD",
        },
    )
    assert "display_html" in out, out
    assert out["display_html"].startswith('<div class="quote-card">')
    assert "AAPL" in out["display_html"]


def test_legacy_tool_legacy_mapping_still_works():
    """A legacy write tool (ack fallback) still gets an ack card."""
    reg = ToolRegistry()
    schema = ToolSchema(
        name="add_watchlist",
        description="fake",
        args_schema=TinyArgs,
        result_schema=TinyResult,
        permission=PermissionType.WRITE.value,
        metadata={"side_effect_mode": "local_transactional"},  # no metadata.display_view
    )
    reg.add(FakeTool(schema))
    sc = ShortCircuit(registry=reg)
    out = sc._attach_display_html(
        "add_watchlist",
        {"status": "ok", "raw": "WATCHLIST_ADDED: AAPL", "id": "watch-1"},
    )
    assert "display_html" in out, out
    assert out["display_html"].startswith('<div class="ack-card">')


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
