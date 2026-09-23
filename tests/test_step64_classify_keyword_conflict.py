"""§0.4.28 — classify() must not bump ``Op.CREATE`` on the bare phrase
"做一个".

User report: '我做一个完整分析：基础面+估值+新闻+近期走势+同业
对比+监控告警' was routed to ``(Intent.ALERT, Op.CREATE)`` →
``create_alert`` HITL gate. Reason: ``_OP_KW[Op.CREATE]`` contained
the substring ``"做一个"`` ("make one / do one"), which substring-
matched inside "我做一个完整分析" — and "完整分析" doesn't contain
a CREATE verb naturally, so the substring match was hijacking the
intent.

Root cause: substring matching is too coarse. Removing ``"做一个"``
from the CREATE keyword set fixes the false-positive without losing
the legitimate "新建一个"/"建一个"/"加一下" CREATE verbs (those are
explicit enough to require the noun context).

The fix:
- Remove ``"做一个"`` from ``_OP_KW[Op.CREATE]`` set.
- Other CREATE verbs (``建一个`` / ``新建一个`` / ``加一下`` /
  ``提醒我`` / ``设置提醒`` / ``加上`` etc.) still hit cleanly.
"""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.core.tier import (
    Intent,
    Op,
    classify,
    classify_multi,
)


# ─────────────────────────────────────────────────────────────────
# §0.4.28 — the bug case
# ─────────────────────────────────────────────────────────────────


def test_complete_analysis_message_routes_to_alert_list_not_create():
    """'完整分析 … 监控告警' must route to ALERT/LIST (read existing
    alerts), NOT ALERT/CREATE (create new)."""
    msg = "我做一个完整分析：基础面+估值+新闻+近期走势+同业对比+监控告警"
    intent, op = classify(msg)
    assert intent == Intent.ALERT
    assert op == Op.LIST, (
        f"classify({msg!r}) bumped to Op.CREATE — bare '做一个' substring "
        f"match is hijacking the read-only intent. See §0.4.28 fix."
    )


def test_complete_analysis_multi_routes_to_single_alert_list():
    """classify_multi must also show the bug is fixed (was emitting
    ALERT/CREATE before the fix)."""
    msg = "我做一个完整分析：基础面+估值+新闻+近期走势+同业对比+监控告警"
    pairs = classify_multi(msg)
    assert all(op == Op.LIST for _, op in pairs), (
        f"classify_multi returned non-LIST pairs: {pairs}"
    )
    # ALERT must appear in multi pairs (it's the entity in the message)
    assert any(intent == Intent.ALERT for intent, _ in pairs), (
        f"ALERT entity not detected in {pairs}"
    )


def test_make_one_analysis_does_not_bump_create():
    """'做一个 分析 / 决定 / 蛋糕' must NOT bump CREATE — the bare
    '做一个' substring is too ambiguous on its own."""
    msg = "做一个 分析"
    intent, op = classify(msg)
    # Some read-only legacy intent (COMPARE / ANALYSIS / UNKNOWN)
    assert op != Op.CREATE, (
        f"classify({msg!r}) bumped to Op.CREATE — should be a read intent"
    )


# ─────────────────────────────────────────────────────────────────
# Legitimate CREATE verbs — these must STILL work
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("msg,reason", [
    ("建一个告警 600036", "explicit '建一个' verb"),
    ("新建一个告警 600036", "explicit '新建一个' verb"),
    ("加一下告警", "explicit '加一下' verb"),
    ("加个告警", "explicit '加个' verb"),
    ("提醒我价格超过 50", "explicit '提醒我' verb"),
    ("提醒一下", "explicit '提醒一下' verb"),
    ("设置提醒", "explicit '设置提醒' verb"),
    ("加上告警", "explicit '加上' verb"),
    ("设个告警", "explicit '设个' verb"),
    ("新建告警 600036 价格超过 40", "explicit '新建' verb"),
    ("create alert for 600036", "English 'create alert'"),
    ("add alert 600036", "English 'add alert'"),
])
def test_explicit_create_verbs_still_route_to_create(msg, reason):
    intent, op = classify(msg)
    assert intent == Intent.ALERT, (
        f"classify({msg!r}) expected Intent.ALERT, got {intent} ({reason})"
    )
    assert op == Op.CREATE, (
        f"classify({msg!r}) expected Op.CREATE, got {op} ({reason})"
    )


# ─────────────────────────────────────────────────────────────────
# Read intent — should stay read-only
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "看一下告警",
    "列出告警",
    "我有什么告警",
    "我的告警",
    "所有告警",
    "show alerts",
    "list alerts",
])
def test_read_only_alert_phrases_route_to_list(msg):
    intent, op = classify(msg)
    assert intent == Intent.ALERT
    assert op == Op.LIST, (
        f"classify({msg!r}) expected Op.LIST, got {op}"
    )


# ─────────────────────────────────────────────────────────────────
# Pin the keyword set — no regression on '做一个'
# ─────────────────────────────────────────────────────────────────


def test_op_create_keyword_set_no_longer_contains_zuo_yi_ge():
    """Source-level pin: ``\"做一个\"`` must NOT appear in
    ``_OP_KW[Op.CREATE]`` — keep this test so a future refactor
    can't accidentally re-add the false-positive substring."""
    from tradingagents.agent_harness.core.tier import _OP_KW
    create_kws = _OP_KW[Op.CREATE]
    assert "做一个" not in create_kws, (
        f"Op.CREATE keyword set re-added '做一个' — this substring "
        f"matches inside benign phrases like '做一个 分析' and "
        f"'我做一个完整分析'. See §0.4.28 regression test. "
        f"Current set: {sorted(create_kws)}"
    )


def test_op_create_keyword_set_retains_explicit_verbs():
    """Source-level pin: the legitimate CREATE verbs must still be
    in the set — preserve the user-facing vocabulary."""
    from tradingagents.agent_harness.core.tier import _OP_KW
    create_kws = _OP_KW[Op.CREATE]
    for kw in ("建一个", "新建", "加一下", "提醒我", "提醒一下",
               "设置提醒", "加上", "设个", "create", "add"):
        assert kw in create_kws, (
            f"Op.CREATE keyword dropped required verb {kw!r}"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
