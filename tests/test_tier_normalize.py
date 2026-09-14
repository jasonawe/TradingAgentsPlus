"""N121 fix: A-share 6-digit codes 自动补 .SS/.SZ 后缀。"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.core.tier import (  # noqa: E402
    extract_symbols,
    normalize_symbol,
)


def test_normalize_shanghai_codes() -> None:
    """600/601/603/605/688 → .SS"""
    for code in ("600036", "601398", "603259", "605358", "688981"):
        assert normalize_symbol(code) == f"{code}.SS", code


def test_normalize_shenzhen_codes() -> None:
    """000/001/002/003/300/301 → .SZ"""
    for code in ("000001", "001979", "002594", "003816", "300750", "301236"):
        assert normalize_symbol(code) == f"{code}.SZ", code


def test_normalize_preserves_already_suffixed() -> None:
    """Already has .SS/.SZ → unchanged."""
    assert normalize_symbol("600036.SS") == "600036.SS"
    assert normalize_symbol("000001.SZ") == "000001.SZ"


def test_normalize_preserves_non_a_share() -> None:
    """Non-6-digit tokens unchanged (NVDA, AAPL, etc.)."""
    assert normalize_symbol("AAPL") == "AAPL"
    assert normalize_symbol("NVDA") == "NVDA"
    assert normalize_symbol("BRK.B") == "BRK.B"
    # 5-digit codes are ambiguous (some ETFs), leave alone
    assert normalize_symbol("51388") == "51388"


def test_normalize_handles_lowercase_and_whitespace() -> None:
    assert normalize_symbol("  600036  ") == "600036.SS"
    assert normalize_symbol("aapl") == "AAPL"


def test_extract_symbols_auto_suffixes_a_shares() -> None:
    out = extract_symbols("分析 600036 和 000001 和 300750")
    assert "600036.SS" in out
    assert "000001.SZ" in out
    assert "300750.SZ" in out


def test_extract_symbols_mixed_a_share_and_us() -> None:
    out = extract_symbols("对比 600036.SS 和 AAPL")
    assert "600036.SS" in out
    assert "AAPL" in out


def test_extract_symbols_no_dupes() -> None:
    """If user types both 600036 and 600036.SS, dedupe to one .SS."""
    out = extract_symbols("对比 600036 和 600036.SS")
    assert out.count("600036.SS") == 1
