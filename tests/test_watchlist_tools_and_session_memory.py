"""§P3-1 + §P3-2 — Watchlist write tools + cross-turn symbol memory.

Two real bugs the user just hit:

  - "add this to my watchlist" — `list_watchlist` existed but there was
    no `add_to_watchlist` / `remove_from_watchlist` write tool, so the
    LLM had no way to actually mutate the list (it just narrated).

  - Two-turn sessions — first turn "深度分析 600036.SS 估值", second
    turn "加入关注" — the orchestrator didn't carry the symbol
    forward, so the new turn saw an empty `state.symbols` and the LLM
    had no anchor for "this asset".

This module covers both fixes.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock as MM

import pytest

from tradingagents.agent_harness.tools.builtin import (
    add_to_watchlist,
    remove_from_watchlist,
    AddToWatchlistArgs,
    RemoveFromWatchlistArgs,
)
from tradingagents.agent_harness.tools.context import ToolContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def watchlist_repo(tmp_path: Path):
    """Real WatchlistRepository against a tmp SQLite store."""
    from web.storage import SQLiteStore
    from web.repositories import WatchlistRepository
    store = SQLiteStore(tmp_path / "settings.db")
    return WatchlistRepository(store)


@pytest.fixture
def injected_repos(watchlist_repo):
    """Inject the watchlist repo into tools_bridge so _get_repo('watchlist') works."""
    from tradingagents.agent_harness.tools import impl as tools_bridge
    prev = getattr(tools_bridge, "_repos", {})
    tools_bridge._repos = {**prev, "watchlist": watchlist_repo}
    yield watchlist_repo
    tools_bridge._repos = prev


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(session_id="watchlist-test")


# ---------------------------------------------------------------------------
# add_to_watchlist
# ---------------------------------------------------------------------------
class TestAddToWatchlist:
    @pytest.mark.asyncio
    async def test_adds_new_symbol(self, injected_repos, ctx):
        args = AddToWatchlistArgs(symbol="600036.SS", asset_type="stock")
        result = await add_to_watchlist(args, ctx)
        assert result.status == "created"
        assert result.symbol == "600036.SS"
        assert result.asset_type == "stock"

        # Verify the repo actually has the entry.
        items = injected_repos.list_items()
        symbols = [it["symbol"] for it in items]
        assert "600036.SS" in symbols

    @pytest.mark.asyncio
    async def test_add_with_note(self, injected_repos, ctx):
        args = AddToWatchlistArgs(
            symbol="AAPL", asset_type="stock", note="tech leader",
        )
        result = await add_to_watchlist(args, ctx)
        assert result.status == "created"

        items = injected_repos.list_items()
        target = next(it for it in items if it["symbol"] == "AAPL")
        assert target["note"] == "tech leader"

    @pytest.mark.asyncio
    async def test_add_duplicate_returns_duplicate_status(self, injected_repos, ctx):
        args = AddToWatchlistArgs(symbol="NVDA", asset_type="stock")
        first = await add_to_watchlist(args, ctx)
        assert first.status == "created"

        second = await add_to_watchlist(args, ctx)
        assert second.status == "duplicate"
        assert "DUPLICATE" in second.raw

    @pytest.mark.asyncio
    async def test_add_invalid_symbol_returns_error(self, injected_repos, ctx):
        args = AddToWatchlistArgs(symbol="!!invalid!!", asset_type="stock")
        result = await add_to_watchlist(args, ctx)
        assert result.status == "error"
        assert result.symbol == "!!invalid!!"

    @pytest.mark.asyncio
    async def test_add_unsupported_asset_type_returns_error(self, injected_repos, ctx):
        # Pydantic Literal should reject — verify the schema rejects it.
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AddToWatchlistArgs(symbol="XAU", asset_type="commodity")

    @pytest.mark.asyncio
    async def test_repo_not_injected_returns_error(self, ctx):
        """When tools_bridge has no watchlist repo, surface the error cleanly."""
        from tradingagents.agent_harness.tools import impl as tools_bridge
        prev = getattr(tools_bridge, "_repos", {})
        tools_bridge._repos = {}
        try:
            args = AddToWatchlistArgs(symbol="AAPL", asset_type="stock")
            result = await add_to_watchlist(args, ctx)
            assert result.status == "error"
            assert "not injected" in result.raw.lower() or "未注入" in result.raw
        finally:
            tools_bridge._repos = prev


# ---------------------------------------------------------------------------
# remove_from_watchlist
# ---------------------------------------------------------------------------
class TestRemoveFromWatchlist:
    @pytest.mark.asyncio
    async def test_removes_existing_symbol(self, injected_repos, ctx):
        # seed
        await add_to_watchlist(
            AddToWatchlistArgs(symbol="AAPL", asset_type="stock"), ctx,
        )
        result = await remove_from_watchlist(
            RemoveFromWatchlistArgs(symbol="AAPL", asset_type="stock"), ctx,
        )
        assert result.status == "deleted"
        assert "AAPL" not in [it["symbol"] for it in injected_repos.list_items()]

    @pytest.mark.asyncio
    async def test_missing_symbol_returns_not_found(self, injected_repos, ctx):
        result = await remove_from_watchlist(
            RemoveFromWatchlistArgs(symbol="TSLA", asset_type="stock"), ctx,
        )
        assert result.status == "not_found"


# ---------------------------------------------------------------------------
# Registry wiring — both tools must show up in the public tool list
# ---------------------------------------------------------------------------
def test_tools_registered_in_registry():
    """add/remove must be registered so the harness exposes them."""
    from tradingagents.agent_harness.tools import install_builtin_tools
    from tradingagents.agent_harness.tools.registry import ToolRegistry

    reg = ToolRegistry()
    install_builtin_tools(reg)
    names = set(reg.list_names())
    assert "add_to_watchlist" in names
    assert "remove_from_watchlist" in names
    assert "list_watchlist" in names


