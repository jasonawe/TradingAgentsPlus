"""Spec §5.1 unit tests — multi-intent read-only routing.

§N1 — fast_route_with_op() detects read-only multi-intent and routes
to Tier.DIRECT with route.multi_pairs populated. Mixed (read+write)
or write-only multi-intent stays at Tier.PLAN_EXECUTE.
"""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.core.tier import (
    Op,
    Tier,
    classify_multi,
    fast_route_with_op,
)


# ---------------------------------------------------------------------------
# classify_multi detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("message,expected_intents", [
    ("看一下 600036.SS 的笔记和告警", {"note", "alert"}),
    ("我的关注 + 600036 的笔记", {"watchlist", "note"}),
    ("看一下我的关注列表", {"watchlist"}),
])
def test_classify_multi_readonly(message, expected_intents):
    pairs = classify_multi(message)
    intents = {p[0].value for p in pairs}
    assert intents == expected_intents
    # All pairs must be LIST/READ op for read-only scenarios.
    assert all(p[1] in (Op.LIST, Op.READ) for p in pairs), (
        f"non-read op in {pairs}"
    )


@pytest.mark.parametrize("message", [
    "把 600036 加关注 + 加笔记",           # write + write
    "看一下 600036 的笔记 + 加个笔记",     # read + write (mixed)
    "新增一个告警 + 修改另一个告警",        # write + write
])
def test_classify_multi_writes(message):
    pairs = classify_multi(message)
    assert any(p[1] in (Op.CREATE, Op.UPDATE, Op.DELETE) for p in pairs), (
        f"no write op detected in {pairs}"
    )


# ---------------------------------------------------------------------------
# fast_route_with_op tier routing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("message", [
    "看一下 600036.SS 的笔记和告警",
    "我的关注 + 600036 的笔记",
])
def test_fast_route_readonly_multi_to_tier1(message):
    route, op = fast_route_with_op(message)
    assert route.tier == Tier.DIRECT, (
        f"expected Tier.DIRECT for read-only multi, got {route.tier.name} "
        f"(reason={route.reason})"
    )
    assert len(route.multi_pairs) >= 2
    # All multi_pairs must be read-only (LIST or READ)
    assert all(p[1] in (Op.LIST, Op.READ) for p in route.multi_pairs)


@pytest.mark.parametrize("message", [
    "把 600036 加关注 + 加笔记",      # write multi
    "看一下 600036 的笔记 + 加个笔记", # mixed read+write
    "新增笔记 + 删除告警",              # write multi
])
def test_fast_route_write_multi_stays_tier2(message):
    route, op = fast_route_with_op(message)
    assert route.tier == Tier.PLAN_EXECUTE, (
        f"expected Tier.PLAN_EXECUTE for write multi, got {route.tier.name} "
        f"(reason={route.reason})"
    )


def test_fast_route_single_readonly_to_tier1():
    route, op = fast_route_with_op("600036 现在多少钱")
    assert route.tier == Tier.DIRECT
    assert len(route.multi_pairs) == 0
    assert op == Op.READ


# ---------------------------------------------------------------------------
# RouteResult.to_dict includes new fields
# ---------------------------------------------------------------------------
def test_route_result_to_dict_includes_new_fields():
    route, _ = fast_route_with_op("看一下 600036 的笔记和告警")
    d = route.to_dict()
    assert "op" in d
    assert d["op"] == "list"  # primary op is LIST
    assert "multi_pairs" in d
    assert isinstance(d["multi_pairs"], list)
    assert all(isinstance(p, (list, tuple)) and len(p) == 2 for p in d["multi_pairs"])
    pairs = {tuple(p) for p in d["multi_pairs"]}
    assert ("note", "list") in pairs
    assert ("alert", "list") in pairs
