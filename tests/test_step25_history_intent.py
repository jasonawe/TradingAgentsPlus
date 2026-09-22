"""Unit test for §Step 25 — HISTORY intent precedence over QUOTE.

User-reported bug: "看一下 513880 最近30天的价格走势图" was routed
to Tier 1 QUOTE (get_quote) because the keyword "价格" matched
_TIER1_KEYWORDS. The user wanted get_history (candles).

Fix: introduce _TIER1_HISTORY_KEYWORDS + a window regex, both checked
before _TIER1_KEYWORDS in classify_intent().
"""
from tradingagents.agent_harness.core.tier import Intent, classify_intent


HISTORY_CASES = [
    "看一下 513880 最近30天的价格走势图",
    "513880 最近 30 天 K线",
    "600036.SS 过去 1 个月 走势",
    "最近一周的行情",
    "history of AAPL",
    "trend for TSLA",
    "过去 5 个交易日 收盘价",
    "BTC 最近 30 天 K 线图",
]

QUOTE_CASES = [
    "招商银行现在多少钱",
    "看一下 600036.SS 价格",
    "今日行情",
    "换手率",
]


def test_history_intent_wins():
    for msg in HISTORY_CASES:
        got = classify_intent(msg)
        assert got == Intent.HISTORY, f"classify_intent({msg!r}) → {got}, expected HISTORY"


def test_quote_still_works():
    for msg in QUOTE_CASES:
        got = classify_intent(msg)
        assert got == Intent.QUOTE, f"classify_intent({msg!r}) → {got}, expected QUOTE"


if __name__ == "__main__":
    test_history_intent_wins()
    test_quote_still_works()
    print(f"OK {len(HISTORY_CASES)+len(QUOTE_CASES)} cases passed")
