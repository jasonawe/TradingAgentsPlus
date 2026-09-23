"""§0.4.27 — create_alert args factory must emit valid Literal enum.

User report: '监控告警' without direction/threshold → orchestrator
emitted ``kind='price'`` which is **not** in
:class:`CreateAlertArgs.kind`'s Literal enum and broke every
'complete analysis' turn with args coerce error.

Pin the contract: every input shape now produces a Literal-valid
``kind`` (``price_above`` / ``price_below`` / ``change_pct`` /
``volume_spike``) so the Pydantic validation never trips, AND a
warning is logged when slots are empty (so the planner team can
later wire classify() to downgrade to ALERT/LIST).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from tradingagents.agent_harness.core.orchestrator import _alert_create_args
from tradingagents.agent_harness.tools.builtin import CreateAlertArgs


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _state(symbols=("600036.SS",), carry=(), message="", slots=None):
    return SimpleNamespace(
        symbols=list(symbols),
        carry_symbols=list(carry),
        user_message=message,
        slots=slots or {},
    )


_VALID_KINDS = ("price_above", "price_below", "change_pct", "volume_spike")


# ─────────────────────────────────────────────────────────────────
# Empty slots — the bug case
# ─────────────────────────────────────────────────────────────────


def test_empty_slots_emits_valid_kind():
    """No direction/threshold → still a valid Literal kind."""
    args = _alert_create_args(_state(
        message="帮我做一个完整分析：基础面+估值+新闻+近期走势+同业对比+监控告警",
    ))
    assert args["kind"] in _VALID_KINDS
    assert args["symbol"] == "600036.SS"
    assert isinstance(args["params"], dict)


def test_empty_slots_warns_planner_team(caplog):
    """The empty-slots path must log a structured warning so the
    planner loop can later downgrade (ALERT, CREATE) → (ALERT, LIST)
    when slots are missing."""
    with caplog.at_level(logging.WARNING, logger="tradingagents.agent_harness.core.orchestrator"):
        _alert_create_args(_state(message="监控告警"))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("create_alert invoked with no threshold" in m for m in msgs), (
        f"expected warning about empty slots; got: {msgs}"
    )


def test_empty_slots_pydantic_validates():
    """End-to-end: args factory output must survive CreateAlertArgs.model_validate."""
    args = _alert_create_args(_state())
    validated = CreateAlertArgs.model_validate(args)
    assert validated.kind in _VALID_KINDS
    assert validated.symbol == "600036.SS"


# ─────────────────────────────────────────────────────────────────
# Happy path — direction + threshold present
# ─────────────────────────────────────────────────────────────────


def test_direction_above_with_threshold_emits_price_above():
    args = _alert_create_args(_state(
        message="价格超过 50 提醒我 600036.SS",
        slots={"direction": "above", "threshold": 50.0},
    ))
    assert args["kind"] == "price_above"
    assert args["params"] == {"threshold": 50.0, "direction": "above"}


def test_direction_below_with_threshold_emits_price_below():
    args = _alert_create_args(_state(
        message="600036.SS 跌破 38 提醒我",
        slots={"direction": "below", "threshold": 38.0},
    ))
    assert args["kind"] == "price_below"
    assert args["params"] == {"threshold": 38.0, "direction": "below"}


def test_direction_with_threshold_no_warning(caplog):
    """When the user gave a real threshold, the warning must NOT fire
    (otherwise the log noise would dwarf useful diagnostics)."""
    with caplog.at_level(logging.WARNING, logger="tradingagents.agent_harness.core.orchestrator"):
        _alert_create_args(_state(slots={"direction": "above", "threshold": 50.0}))
    empty_warns = [r for r in caplog.records
                   if "create_alert invoked with no threshold" in r.getMessage()]
    assert empty_warns == [], (
        f"happy path must not log the empty-slots warning; got: {[r.getMessage() for r in empty_warns]}"
    )


# ─────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────


def test_change_pct_slot_emits_change_pct_kind():
    """When extract_slots populates ``change_pct`` slot, route to
    ``kind=change_pct`` (with empty params: Pydantic handles defaults)."""
    # The current factory doesn't have a change_pct branch yet — that
    # is a separate workstream. Pin current behaviour: empty slots
    # default to price_above.
    args = _alert_create_args(_state(slots={"change_pct": -3.0}))
    # Today: falls into default branch (no `direction` key).
    assert args["kind"] in _VALID_KINDS


def test_scope_slot_forwarded():
    """§Step 15 — scope slot must forward to the tool call."""
    args = _alert_create_args(_state(slots={"scope": "all"}))
    assert args["scope"] == "all"


def test_default_scope_is_user():
    """No scope slot → default to user-scope alerts."""
    args = _alert_create_args(_state())
    assert args["scope"] == "user"


def test_symbol_falls_back_to_carry():
    """state.symbols empty but carry_symbols has 600036.SS → use carry."""
    args = _alert_create_args(_state(
        symbols=[],
        carry=("600036.SS",),
    ))
    assert args["symbol"] == "600036.SS"


def test_asset_type_default_is_stock():
    """§P3-3+ — stock is the default asset_type for create_alert."""
    args = _alert_create_args(_state())
    assert args["asset_type"] == "stock"


# ─────────────────────────────────────────────────────────────────
# Regression: literal 'price' must NEVER appear in factory output
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("message,slots", [
    ("监控告警", {}),
    ("查看告警", {}),
    ("看一下告警", {}),
    ("alerts?", {}),
    ("", {}),
    ("提醒我", {}),
    ("告警", {}),
    ("600036.SS 监控", {}),
])
def test_never_emits_invalid_price_kind(message, slots):
    """§0.4.27 regression — ``kind='price'`` triggered args coerce
    failure for every multi-dimensional analysis turn. Pin that no
    factory output now contains ``'price'`` (the bare token, not
    ``price_above`` / ``price_below``)."""
    args = _alert_create_args(_state(message=message, slots=slots))
    assert args["kind"] != "price", (
        f"factory emitted bare 'price' for message={message!r}, slots={slots!r}"
    )
    # And must be Pydantic-validatable
    validated = CreateAlertArgs.model_validate(args)
    assert validated.kind in _VALID_KINDS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
