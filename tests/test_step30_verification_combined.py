"""Step 30 — verification.py combines citation + claim_audit + LLM judge.

The LLM judge verdict is the primary signal. But we now discount /
force-ungrounded when:
  - citation score is very low (< 0.4) and data tools were used
  - claim audit finds unsupported numbers AND answer contains a
    forward-looking claim (forecast / estimate / 预计 / 预计将)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class _StubResp:
    def __init__(self, content):
        self.content = content


def _make_provider(content: str):
    p = MagicMock()
    p.complete_text = MagicMock(return_value=_StubResp(content))
    return p


def _judge_json(score: float, grounded_issues=None) -> str:
    import json
    return json.dumps({
        "score": score,
        "issues": grounded_issues or [],
        "suggestion": "",
        "reasoning": "stub",
    })


async def _run_verify(answer, tool_results, judge_score):
    from tradingagents.agent_harness.core.verification import Verifier
    v = Verifier(judge_factory=MagicMock(is_configured=MagicMock(return_value=True)))
    v.judge_factory.make = MagicMock(return_value=_make_provider(_judge_json(judge_score)))
    return await v.verify_l3(
        user_query="test",
        tool_results=tool_results,
        llm_answer=answer,
    )


def test_citation_missing_with_data_tools_forces_low_score():
    """When the answer omits the 来源 block and data tools were used,
    combined score should be <= 0.5 even if LLM judge says 0.95."""
    import asyncio
    answer = "## 数据事实\n股价 40.71\n## 方向性建议\n观望"  # NO citation block
    tool_results = [
        {"name": "get_quote", "result": {"price": 40.71}},
    ]
    res = asyncio.run(_run_verify(answer, tool_results, judge_score=0.95))
    assert res.ok is False
    assert res.details["citation_score"] < 0.4
    assert "citation_score" in res.details["issues"][0]


def test_forward_claim_with_fabricated_number_hard_ungrounded():
    """LLM says 'PE 预期 12.5x' but tool only had 40.71. Hard fail."""
    import asyncio
    answer = (
        "## 数据事实\n股价 40.71\n"
        "PE 预期 12.5x (来源: 不明)\n"  # forward + fabricated
        "## 来源\n> get_quote\n"
    )
    tool_results = [{"name": "get_quote", "result": {"price": 40.71}}]
    res = asyncio.run(_run_verify(answer, tool_results, judge_score=0.95))
    assert res.ok is False
    # Either hard_ungrounded or combined score < 0.7
    details = res.details
    assert details["claim_audit_unsupported"]  # at least 12.5 unsupported
    assert details["claim_audit_score"] < 1.0


def test_full_match_passes():
    import asyncio
    answer = (
        "## 数据事实\n股价 40.71, 涨 +1.04%\n"
        "## 来源\n> get_quote\n"
    )
    tool_results = [{"name": "get_quote", "result": {"price": 40.71, "change_pct": 1.04}}]
    res = asyncio.run(_run_verify(answer, tool_results, judge_score=0.9))
    assert res.ok is True
    assert res.details["citation_score"] == 1.0
    assert res.details["claim_audit_score"] == 1.0


def test_combined_breakdown_in_details():
    """Surface all three signals in details for UI badge."""
    import asyncio
    answer = "## 数据事实\n股价 40.71\n## 来源\n> get_quote\n"
    tool_results = [{"name": "get_quote", "result": {"price": 40.71}}]
    res = asyncio.run(_run_verify(answer, tool_results, judge_score=0.9))
    d = res.details
    assert "citation_score" in d
    assert "claim_audit_score" in d
    assert "llm_judge_score" in d
    assert "claim_audit_unsupported" in d


def test_no_data_tools_citation_irrelevant():
    """If only CRUD tools were used (no data tools), citation is
    irrelevant — should pass."""
    import asyncio
    answer = "笔记已删除 (600036.SS)"  # trivial CRUD ack, no data
    tool_results = [{"name": "delete_note", "result": {"status": "deleted"}}]
    res = asyncio.run(_run_verify(answer, tool_results, judge_score=0.9))
    assert res.ok is True
    # citation_score should be 1.0 because no data tools
    assert res.details["citation_score"] == 1.0
