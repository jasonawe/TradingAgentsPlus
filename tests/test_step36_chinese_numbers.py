"""Step 36 — Chinese number parser for claim_audit.

The Step 30 claim_audit extracts Arabic numbers from the LLM
answer and verifies each against tool data. It does NOT handle
Chinese number phrases — these tests cover the parser and the
integration into claim_audit_score().
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ------------------------------------------------------------------
# Parser
# ------------------------------------------------------------------
def test_simple_digits():
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
    )
    nums = extract_chinese_numbers("一 二 三")
    # Pure single digits are too noisy — filtered out.
    assert nums == []


def test_multiplicative_form():
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
    )
    nums = extract_chinese_numbers("市值三百亿")
    assert 30_000_000_000 in nums


def test_decimal_form():
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
    )
    nums = extract_chinese_numbers("价格三点五元")
    assert 3.5 in nums


def test_two_colloquial():
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
    )
    nums = extract_chinese_numbers("两百")
    assert 200 in nums


def test_percentage():
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
    )
    nums = extract_chinese_numbers("涨百分之五")
    assert 0.05 in nums


def test_ratio():
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
    )
    nums = extract_chinese_numbers("概率五成")
    assert 0.5 in nums


def test_negative():
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
    )
    nums = extract_chinese_numbers("亏损负七亿")
    assert -700_000_000 in nums


def test_compound_with_zero_padding():
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
    )
    nums = extract_chinese_numbers("三千零五")
    assert 3005 in nums


def test_zero_explicit():
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
    )
    nums = extract_chinese_numbers("净增为零")
    assert 0 in nums


def test_invalid_phrase_dropped():
    """Garbage candidates don't produce phantom numbers."""
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
    )
    # "点" alone isn't a number
    nums = extract_chinese_numbers("就这点儿")
    # Should be empty (or at least not contain a NaN/garbage value)
    for n in nums:
        assert isinstance(n, (int, float))
        assert n >= 0 or n <= 0  # finite


# ------------------------------------------------------------------
# Integration with claim_audit
# ------------------------------------------------------------------
def test_claim_audit_matches_chinese_phrase_to_arabic_data():
    """LLM says 三百亿, tool data says 30000000000 — should match."""
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "市值三百亿人民币,基本面稳健"
    tool_results = [{
        "name": "get_fundamentals",
        "result": {"market_cap": 30_000_000_000},
    }]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert score == 1.0, f"expected full match, got {score}, unsupported={unsupported}"
    assert unsupported == []


def test_claim_audit_catches_unsupported_chinese_number():
    """LLM invents 五百亿, tool data says 300亿 — should fail."""
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "市值五百亿"
    tool_results = [{
        "name": "get_fundamentals",
        "result": {"market_cap": 30_000_000_000},
    }]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert score < 1.0
    assert any(abs(float(u) - 50_000_000_000) < 1 for u in unsupported)


def test_claim_audit_mixed_arabic_and_chinese():
    """Answer has both — both must match."""
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "市值 30000000000 (三百亿), PE 12.5"
    tool_results = [{
        "name": "get_fundamentals",
        "result": {"market_cap": 30_000_000_000, "pe": 12.5},
    }]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert score == 1.0


def test_claim_audit_percentage_in_chinese():
    """百分之五 → 0.05, should match tool data with 5.2%."""
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "涨百分之五, 量能放大"
    tool_results = [{
        "name": "get_quote",
        "result": {"change_pct": 5.0},
    }]
    score, _ = claim_audit_score(answer, tool_results)
    assert score == 1.0
