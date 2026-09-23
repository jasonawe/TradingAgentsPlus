"""§0.4.16 — carry-forward reaches Tier.DIRECT for implicit-asset queries.

User reported: after '看一下 AAPL 的基本面数据' (Tier 1 short-circuit
on get_fundamentals), the follow-up '看一下这个资产最近30天的价格走
势图' failed with 'please tell me the ticker' — even though the
previous turn clearly anchored AAPL via L2 session_ctx.

Root cause: ``fast_route_with_op`` populates ``carry_symbols`` and
calls ``fast_route(message)`` WITHOUT passing them. ``fast_route``
re-extracts symbols from the bare message ("这个资产..." has no
ticker) → empty list → Tier.DIRECT predicate fails → drops to the
default PLAN_EXECUTE fallback. The carry-forward assignment
happens AFTER fast_route returns, too late to fix the tier
selection. The LLM planner then runs, fails to emit a plan (demo
pronoun has no actionable ticker), and the user gets a "please
clarify" response.

Fix: thread ``carry_symbols`` into ``fast_route`` so the
Tier.DIRECT predicate sees the merged symbol list. Read-only
intents (QUOTE / HISTORY / NEWS / FUNDAMENTALS / ALPHA) then
short-circuit cleanly with carry-forward supplying the implicit
ticker.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_history_with_carry_forward_reaches_tier_direct():
    """The user's exact regression: '看一下这个资产最近30天走势' +
    carry-forward AAPL must Tier.DIRECT to get_history, NOT
    PLAN_EXECUTE."""
    from tradingagents.agent_harness.core.tier import (
        Intent, Tier, fast_route_with_op,
    )
    route, _op = fast_route_with_op(
        "看一下这个资产最近30天的价格走势图",
        carry_symbols=["AAPL"],
    )
    assert route.intent == Intent.HISTORY, (
        f"intent should be HISTORY for '走势' query; got {route.intent}"
    )
    assert route.tier == Tier.DIRECT, (
        f"carry-forward + history should short-circuit to Tier 1; "
        f"got tier={route.tier}, reason={route.reason}"
    )
    assert "AAPL" in route.symbols


def test_quote_with_carry_forward_reaches_tier_direct():
    """Same fix on QUOTE: '看一下这个资产的价格' + carry-forward
    600036.SS must reach Tier.DIRECT."""
    from tradingagents.agent_harness.core.tier import (
        Intent, Tier, fast_route_with_op,
    )
    route, _op = fast_route_with_op(
        "看一下这个资产的价格",
        carry_symbols=["600036.SS"],
    )
    assert route.intent == Intent.QUOTE
    assert route.tier == Tier.DIRECT, (
        f"got tier={route.tier}, reason={route.reason}"
    )
    assert "600036.SS" in route.symbols


def test_explicit_symbol_still_wins_over_carry():
    """Sanity: explicit message symbols take precedence over carry-
    forward. The §Step 20 ordering must NOT regress when we add the
    carry-aware path inside fast_route."""
    from tradingagents.agent_harness.core.tier import (
        Intent, Tier, fast_route_with_op,
    )
    route, _op = fast_route_with_op(
        "看一下 NVDA 的价格",
        carry_symbols=["AAPL"],
    )
    # NVDA is in the message, not AAPL; NVDA wins.
    assert "NVDA" in route.symbols
    assert route.tier == Tier.DIRECT


def test_no_carry_no_symbol_falls_back_to_plan_execute():
    """Without carry-forward AND no ticker in the message, ambiguous
    queries still go to Tier 2 (LLM clarification round)."""
    from tradingagents.agent_harness.core.tier import (
        Intent, Tier, fast_route_with_op,
    )
    route, _op = fast_route_with_op("看一下走势")
    # No symbol anywhere → Tier 2 fallback.
    assert route.tier == Tier.PLAN_EXECUTE
    assert route.symbols == []


def test_fundamentals_with_carry_forward_reaches_tier_direct():
    """'这家公司的基本面' + carry-forward AAPL → Tier.DIRECT
    fundamentals."""
    from tradingagents.agent_harness.core.tier import (
        Intent, Tier, fast_route_with_op,
    )
    route, _op = fast_route_with_op(
        "这家公司的基本面",
        carry_symbols=["AAPL"],
    )
    assert route.intent == Intent.FUNDAMENTALS
    assert route.tier == Tier.DIRECT
    assert "AAPL" in route.symbols


def test_news_with_carry_forward_reaches_tier_direct():
    """'最近有什么新闻' + carry-forward 600036.SS → Tier.DIRECT
    news."""
    from tradingagents.agent_harness.core.tier import (
        Intent, Tier, fast_route_with_op,
    )
    route, _op = fast_route_with_op(
        "最近有什么新闻",
        carry_symbols=["600036.SS"],
    )
    assert route.intent == Intent.NEWS
    assert route.tier == Tier.DIRECT
    assert "600036.SS" in route.symbols


if __name__ == "__main__":
    test_history_with_carry_forward_reaches_tier_direct()
    test_quote_with_carry_forward_reaches_tier_direct()
    test_explicit_symbol_still_wins_over_carry()
    test_no_carry_no_symbol_falls_back_to_plan_execute()
    test_fundamentals_with_carry_forward_reaches_tier_direct()
    test_news_with_carry_forward_reaches_tier_direct()
    print("ALL §0.4.16 carry-forward tests passed")
