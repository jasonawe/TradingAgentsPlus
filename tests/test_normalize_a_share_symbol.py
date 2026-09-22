"""Unit test for _normalize_a_share_symbol.

User-reported bug: typing a bare 6-digit code like "513880" returned
NO_DATA because no provider resolves "513880" (canonical Yahoo
symbol is 513880.SS). The fix auto-appends the SH/SZ exchange
suffix at the tool layer.
"""
from tradingagents.agent_harness.tools.builtin import _normalize_a_share_symbol as n


CASES = [
    # Bare A-share codes → auto-suffix
    ("513880", "513880.SS"),    # ETF (上交所)
    ("600036", "600036.SS"),    # 主板 (上交所)
    ("688825", "688825.SS"),    # 科创板 (上交所)
    ("600519", "600519.SS"),    # 茅台
    ("000001", "000001.SZ"),    # 平安银行 (深交所)
    ("002415", "002415.SZ"),    # 海康威视
    ("300750", "300750.SZ"),    # 宁德时代 (创业板)
    ("301236", "301236.SZ"),    # 创业板延伸

    # Already suffixed → unchanged
    ("513880.SS", "513880.SS"),
    ("600036.SZ", "600036.SZ"),

    # Foreign / crypto / index → unchanged (no digits-only rule)
    ("NVDA",    "NVDA"),
    ("AAPL",   "AAPL"),
    ("BTC-USD","BTC-USD"),
    ("^GSPC",  "^GSPC"),
    ("GC=F",   "GC=F"),

    # Edge cases
    ("",       ""),
    (None,     None),
    ("1234",   "1234"),          # wrong length
    ("abc",    "abc"),
    (" 600036 ", "600036.SS"),   # whitespace trim
]


def test_normalize():
    for raw, expected in CASES:
        got = n(raw)
        assert got == expected, f"normalize({raw!r}) → {got!r}, expected {expected!r}"
    print(f"OK {len(CASES)} cases passed")


if __name__ == "__main__":
    test_normalize()
