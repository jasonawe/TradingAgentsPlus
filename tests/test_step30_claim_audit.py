"""Step 30 — claim audit detector.

The synthesizer LLM sometimes fabricates numbers. The existing
citation_score checks if the LLM listed which tools it used; this
checks if the numbers in the prose actually came from those tools.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_extract_numbers_strips_currency_and_pct():
    from tradingagents.agent_harness.verification.claim_audit import (
        extract_numbers,
    )
    nums = extract_numbers("股价 ¥40.71, 涨 +1.04%, PE 12.5")
    # Year-like 4-digit numbers should be filtered
    assert "40.71" in nums
    # Either format acceptable (we keep the raw + sign / %)
    assert "12.5" in nums


def test_extract_numbers_skips_years():
    from tradingagents.agent_harness.verification.claim_audit import (
        extract_numbers,
    )
    nums = extract_numbers("2026 年 9 月, 价格 40.71")
    assert "40.71" in nums
    # 2026 should NOT appear (it's a year)
    assert "2026" not in nums


def test_claim_audit_score_full_match():
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "股价 40.71, 涨 1.04%, 成交量 20714047"
    tool_results = [{
        "name": "get_quote",
        "result": {"price": 40.71, "change_pct": 1.04, "volume": 20714047},
    }]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert score == 1.0
    assert unsupported == []


def test_claim_audit_score_detects_fabricated_number():
    """LLM invents PE = 12.5 which isn't in the tool result."""
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "股价 40.71, PE 12.5 (fabricated)"
    tool_results = [{
        "name": "get_quote",
        "result": {"price": 40.71},  # no PE in data
    }]
    score, unsupported = claim_audit_score(answer, tool_results)
    # 40.71 is grounded, 12.5 is not → 0.5
    assert score == 0.5
    assert "12.5" in unsupported


def test_claim_audit_score_no_numbers():
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "暂时无法给出准确数字"
    tool_results = [{"name": "get_quote", "result": {"price": 40.71}}]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert score == 1.0
    assert unsupported == []


def test_claim_audit_score_no_tool_results():
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "股价 40.71, PE 12.5"
    score, unsupported = claim_audit_score(answer, [])
    assert score == 0.0
    assert "40.71" in unsupported


def test_claim_audit_score_percentage_match():
    """Answer "5.2%" should match tool data "5.2"."""
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "涨幅 5.2%"
    tool_results = [{"result": {"change_pct": 5.2}}]
    score, _ = claim_audit_score(answer, tool_results)
    assert score == 1.0


def test_has_forward_claim_chinese():
    from tradingagents.agent_harness.verification.claim_audit import (
        has_forward_claim,
    )
    assert has_forward_claim("预计 2026 年净利润增长 12%")
    assert has_forward_claim("PE 预期 12x")
    assert not has_forward_claim("当前价格 40.71")


def test_has_forward_claim_english():
    from tradingagents.agent_harness.verification.claim_audit import (
        has_forward_claim,
    )
    assert has_forward_claim("target price $185")
    assert has_forward_claim("FY26 EPS estimate $6.40")
    assert not has_forward_claim("current price $185")


def test_claim_audit_handles_nested_dicts():
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "PE 12.5, 收入 1000亿"
    tool_results = [{
        "result": {
            "fundamentals": {
                "income": 1000,  # in 亿元
                "ratios": {"pe": 12.5},
            }
        }
    }]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert score == 1.0
    assert unsupported == []
