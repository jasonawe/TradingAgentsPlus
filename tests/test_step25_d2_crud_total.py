"""Step 25 — D2 CRUD layer migration: 100% tool coverage.

After Step 24 (data layer) and Step 25 (CRUD layer), every
registered tool carries D2 metadata. This test enforces that:
1. Every tool has non-empty metadata
2. capability enum is from the standard list
3. category ∈ {data, crud, meta}
4. display_view is a known renderer key (or "ack" for write tools)
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _reg():
    from tradingagents.agent_harness.tools import ToolRegistry
    from tradingagents.agent_harness.tools.builtin import install_builtin_tools
    reg = ToolRegistry()
    install_builtin_tools(reg)
    return reg


_KNOWN_CAPABILITIES = {
    "quote", "history", "fundamentals", "news", "alpha",
    "note", "alert", "watchlist", "scheduled", "run", "report",
    "meta", "report_read",
}
_KNOWN_CATEGORIES = {"data", "crud", "meta"}
_KNOWN_DISPLAY_VIEWS = {
    "quote", "history", "fundamentals", "news", "alpha_list",
    "ack", "list", "report_read", "report_list",
}


def test_every_tool_has_metadata():
    reg = _reg()
    missing = [n for n in reg.list_names() if not reg.get(n).schema.metadata]
    assert not missing, f"tools without metadata: {missing}"


def test_every_capability_is_known():
    reg = _reg()
    bad = []
    for n in reg.list_names():
        caps = reg.get(n).schema.metadata.get("capabilities", [])
        for c in caps:
            if c not in _KNOWN_CAPABILITIES:
                bad.append((n, c))
    assert not bad, f"unknown capability: {bad}"


def test_every_category_is_known():
    reg = _reg()
    bad = [
        n for n in reg.list_names()
        if reg.get(n).schema.metadata.get("category") not in _KNOWN_CATEGORIES
    ]
    assert not bad, f"unknown category on: {bad}"


def test_every_display_view_is_known():
    reg = _reg()
    bad = [
        n for n in reg.list_names()
        if reg.get(n).schema.metadata.get("display_view") not in _KNOWN_DISPLAY_VIEWS
    ]
    assert not bad, f"unknown display_view on: {bad}"


def test_at_least_30_tools_migrated():
    """We expect ~33 tools; assert at least 30 to catch large regressions."""
    reg = _reg()
    migrated = sum(1 for n in reg.list_names() if reg.get(n).schema.metadata)
    assert migrated >= 30, f"only {migrated} migrated"


def test_data_layer_count():
    reg = _reg()
    data = [n for n in reg.list_names() if reg.get(n).schema.metadata.get("category") == "data"]
    # 8 data tools (Step 24)
    assert len(data) == 8, f"expected 8 data tools, got {len(data)}"


def test_crud_layer_count():
    reg = _reg()
    crud = [n for n in reg.list_names() if reg.get(n).schema.metadata.get("category") == "crud"]
    # 24 CRUD tools: note(5) + alert(4) + scheduled(4+1 run_scheduled_task) +
    # watchlist(3) + run(4) + report(2)
    # Actually: note 5, alert 4, scheduled 4+1=5, watchlist 3, run 4,
    # report 2 = 23
    assert len(crud) >= 20, f"expected >= 20 crud tools, got {len(crud)}"


def test_write_tools_use_ack_view():
    """create_*/update_*/delete_*/add_*/remove_*/run_*/cancel_* use ack."""
    reg = _reg()
    write_tools = [n for n in reg.list_names() if any(
        n.startswith(p) for p in (
            "create_", "update_", "delete_", "add_", "remove_",
            "run_", "cancel_",
        )
    )]
    bad = [
        n for n in write_tools
        if reg.get(n).schema.metadata.get("display_view") != "ack"
    ]
    assert not bad, f"write tools not using 'ack' view: {bad}"


def test_list_tools_use_list_view():
    """CRUD-side list_* tools use display_view=list. list_alpha_factors
    is a data tool and uses alpha_list — exempt."""
    reg = _reg()
    list_tools = [
        n for n in reg.list_names()
        if n.startswith("list_") and n != "list_alpha_factors"
    ]
    bad = [
        n for n in list_tools
        if reg.get(n).schema.metadata.get("display_view") != "list"
    ]
    assert not bad, f"list tools not using 'list' view: {bad}"
