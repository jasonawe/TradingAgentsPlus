"""Spec Step 20 — slot / symbol carry-forward across turns.

When the user message has no ticker ("加入我的关注", "看一下估值",
"分析这家") but the previous turn mentioned 600036.SS, the router
must surface 600036.SS in route.symbols so the CRUD dispatch
args factory gets a non-empty ``symbol`` field.

Without this fix, every CRUD write that follows a discussion of an
asset fails with an empty symbol and the LLM hallucinates an
answer.
"""
from __future__ import annotations

from tradingagents.agent_harness.core.tier import (
    Intent,
    Op,
    fast_route_with_op,
)


def test_carry_forward_used_when_message_has_no_ticker():
    """Message with no ticker → route.symbols gets carry values."""
    route, op = fast_route_with_op(
        "加入我的关注",
        carry_symbols=["600036.SS"],
    )
    assert "600036.SS" in route.symbols


def test_carry_forward_does_not_override_explicit_ticker():
    """Message with its own ticker → carry is ignored."""
    route, op = fast_route_with_op(
        "把 600519.SS 加入我的关注",
        carry_symbols=["600036.SS"],
    )
    # 600519 wins — explicit message intent overrides carry.
    assert "600519.SS" in route.symbols
    assert "600036.SS" not in route.symbols


def test_no_carry_symbols_route_unchanged():
    """Without carry, route.symbols stays empty for tickerless input."""
    route, op = fast_route_with_op("加入我的关注")
    assert route.symbols == []


def test_carry_forward_with_lookback_query():
    """Look-back style: "看一下这个资产的笔记" + carry → routed to NOTE/LIST."""
    route, op = fast_route_with_op(
        "看一下这个资产的笔记",
        carry_symbols=["600036.SS"],
    )
    assert Intent.NOTE in (route.intent, Intent.WATCHLIST) or route.intent == Intent.NOTE


def test_carry_forward_with_analysis_intent():
    """Deep analysis on implicit asset."""
    route, op = fast_route_with_op(
        "深度分析这家",
        carry_symbols=["NVDA"],
    )
    assert "NVDA" in route.symbols
