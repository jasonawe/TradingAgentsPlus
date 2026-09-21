"""§Step 22 — gate trade_date extraction on analysis-context.

Regression: "今天周几" used to route to (RUN, CREATE) because the
slot extractor blindly filled trade_date whenever "今天" appeared.
After the fix, plain date questions stay outside the trading pipeline.
"""
import datetime as _dt

from tradingagents.agent_harness.core.tier import (
    extract_slots,
    classify,
    Intent,
    Op,
)


_TODAY = _dt.date.today().isoformat()


def _has_trade_date(msg: str) -> bool:
    return "trade_date" in extract_slots(msg)


# ── Plain date questions should NOT fill trade_date ────────────
def test_today_dow_does_not_set_trade_date():
    assert not _has_trade_date("今天周几"), "今天周几 should not produce trade_date"


def test_now_time_does_not_set_trade_date():
    assert not _has_trade_date("现在几点")


def test_today_weather_does_not_set_trade_date():
    assert not _has_trade_date("今天天气怎么样")


def test_tomorrow_dow_does_not_set_trade_date():
    assert not _has_trade_date("明天星期几")


# ── Analysis-shaped messages MUST still fill trade_date ────────
def test_today_with_ticker_sets_trade_date():
    slots = extract_slots("今天 600036 怎么样")
    assert slots.get("trade_date") == _TODAY, slots


def test_today_with_analysis_verb_sets_trade_date():
    slots = extract_slots("今天分析大盘")
    assert slots.get("trade_date") == _TODAY, slots


def test_tomorrow_with_ticker_sets_trade_date():
    tomorrow = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    slots = extract_slots("明天分析 600036")
    assert slots.get("trade_date") == tomorrow, slots


def test_explicit_iso_with_ticker_sets_trade_date():
    slots = extract_slots("用 2026-09-01 分析 600036")
    assert slots.get("trade_date") == "2026-09-01", slots


# ── End-to-end routing: simple question must NOT be (RUN, CREATE)
def test_plain_date_question_not_routed_to_run_create():
    intent, op = classify("今天周几")
    assert (intent, op) != (Intent.RUN, Op.CREATE), \
        f"plain date question routed to (RUN, CREATE): {intent, op}"


def test_plain_today_with_ticker_routes_to_run_create():
    # Regression guard: ticker+今天 should still trigger the
    # §Step3 slot-aware override.
    intent, op = classify("今天分析 600036")
    assert (intent, op) == (Intent.RUN, Op.CREATE), f"got {intent, op}"
