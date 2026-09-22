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

# ── §Step 24 — claim_audit regressions (substr + sign + tokenisation) ──
def test_claim_audit_handles_leading_plus_sign():
    """``+0.22%`` in the answer must match ``0.22`` in tool data.

    Previously the substring match used ``num`` directly which left the
    leading ``+`` in place; the float comparison path was skipped
    because haystack strings (``"change_pct=0.22"``) aren't a single
    parseable float.
    """
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    answer = "| 600000.SS | 浦发银行 | +0.22% |"
    tool_results = [
        {"name": "get_quote", "result": "change_pct=0.22 price=9.03"}
    ]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert "+0.22%" not in unsupported, unsupported
    assert score == 1.0, (score, unsupported)


def test_claim_audit_tokenises_multi_value_haystack():
    """When the tool result is a ``repr(...)`` blob with many numbers,
    every number inside must be matchable against the answer.
    Previously the whole blob was passed to ``_to_float`` which
    returned None, so even an exact-match number (``26.90`` vs
    ``26.9``) was reported as unsupported.
    """
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    blob = (
        "symbol='600030.SS' price=26.9 open=26.83 high=27.03 "
        "change_pct=0.6 amplitude=1.05"
    )
    answer = "中信证券 | 26.90 | 1.05% | +0.60% |"
    tool_results = [{"name": "get_quote", "result": blob}]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert "26.90" not in unsupported, unsupported
    assert "1.05" not in unsupported, unsupported
    assert score == 1.0, (score, unsupported)


def test_claim_audit_full_six_stock_compare():
    """Real end-to-end: 6 get_quote ``repr(...)`` blobs vs a 6-row
    table answer. Score must be 1.0 with no unsupported numbers.
    """
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    results = [
        "symbol='600036.SS' price=40.92 change_pct=-0.29 amplitude=0.97",
        "symbol='600000.SS' price=9.03 change_pct=0.22 amplitude=1.11",
        "symbol='600030.SS' price=26.9 change_pct=0.6 amplitude=1.05",
        "symbol='600418.SS' price=21.32 change_pct=10.01 amplitude=10.68",
        "symbol='600031.SS' price=17.72 change_pct=-0.23 amplitude=1.13",
        "symbol='513880.SS' price=2.15 change_pct=1.18 amplitude=0.85",
    ]
    answer = (
        "| 600036.SS | 40.92 | 0.97% | -0.29% |\n"
        "| 600000.SS | 9.03 | 1.11% | +0.22% |\n"
        "| 600030.SS | 26.90 | 1.05% | +0.60% |\n"
        "| 600418.SS | 21.32 | 10.68% | +10.01% |\n"
        "| 600031.SS | 17.72 | 1.13% | -0.23% |\n"
        "| 513880.SS | 2.15 | 0.85% | +1.18% |"
    )
    tool_results = [{"name": "get_quote", "result": r} for r in results]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert score == 1.0, (score, unsupported)
    assert unsupported == [], unsupported


def test_claim_audit_still_flags_truly_fabricated_number():
    """Negative regression: a number that's NOT in any tool result
    must still be reported as unsupported.
    """
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )
    tool_results = [{"name": "get_quote", "result": "price=40.92"}]
    answer = "价格 99.99 元"  # 99.99 is fabricated
    score, unsupported = claim_audit_score(answer, tool_results)
    assert "99.99" in unsupported, unsupported

# ── §Step 24 P2 — Pydantic BaseModel result handling ──
def test_claim_audit_walks_pydantic_basemodel():
    """Real production path: get_quote returns a Pydantic
    ``QuoteResult`` stored at ``result["result"]``. The walker must
    recurse into it via ``model_dump()`` so its numeric fields
    (price / change_pct / amplitude / …) reach the haystack.
    """
    from pydantic import BaseModel
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )

    class QuoteResult(BaseModel):
        symbol: str
        price: float
        change_pct: float
        amplitude: float

    quote = QuoteResult(symbol="600036.SS", price=40.92, change_pct=-0.29, amplitude=0.97)
    answer = "招商银行 40.92 元,当日 -0.29%,振幅 0.97%。"
    tool_results = [{"name": "get_quote", "result": quote}]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert score == 1.0, (score, unsupported)
    assert unsupported == [], unsupported


def test_claim_audit_pydantic_full_compare_e2e():
    """Six Pydantic QuoteResult + 6-row answer → score 1.0, 0 unsupported.
    Mirrors production ``state.tool_results`` shape."""
    from pydantic import BaseModel
    from tradingagents.agent_harness.verification.claim_audit import (
        claim_audit_score,
    )

    class QuoteResult(BaseModel):
        symbol: str
        price: float
        change_pct: float
        amplitude: float

    quotes = [
        QuoteResult(symbol="600036.SS", price=40.82, change_pct=-0.54, amplitude=0.97),
        QuoteResult(symbol="600000.SS", price=9.04, change_pct=0.33, amplitude=1.11),
        QuoteResult(symbol="600030.SS", price=26.89, change_pct=0.56, amplitude=1.05),
        QuoteResult(symbol="600418.SS", price=21.32, change_pct=10.01, amplitude=10.68),
        QuoteResult(symbol="600031.SS", price=17.72, change_pct=-0.23, amplitude=1.13),
        QuoteResult(symbol="513880.SS", price=2.151, change_pct=1.22, amplitude=0.94),
    ]
    answer = (
        "| 600036.SS | 40.82 | 0.97% | -0.54% |\n"
        "| 600000.SS | 9.04 | 1.11% | +0.33% |\n"
        "| 600030.SS | 26.89 | 1.05% | +0.56% |\n"
        "| 600418.SS | 21.32 | 10.68% | +10.01% |\n"
        "| 600031.SS | 17.72 | 1.13% | -0.23% |\n"
        "| 513880.SS | 2.151 | 0.94% | +1.22% |"
    )
    tool_results = [{"name": "get_quote", "result": q} for q in quotes]
    score, unsupported = claim_audit_score(answer, tool_results)
    assert score == 1.0, (score, unsupported)
    assert unsupported == [], unsupported

