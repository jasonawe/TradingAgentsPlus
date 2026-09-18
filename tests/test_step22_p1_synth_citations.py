"""Spec Step 22 P1 — synthesizer citation enforcement.

Two halves:

1. The synthesizer system prompt requires a `> 来源: ...` block
   listing every tool that contributed data. We test the prompt
   string directly.

2. The L3 judge down-weights answers without citation when the
   underlying query is data-bearing (i.e. tool_results has real
   data, not trivial acks).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_synth_system_prompt_requires_sources():
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    sys_prompt = Orchestrator._SYNTH_SYSTEM
    assert "来源" in sys_prompt or "source" in sys_prompt.lower()
    assert "引用" in sys_prompt or "citation" in sys_prompt.lower() or "data" in sys_prompt.lower()


def test_citation_detector_passes_when_present():
    """An answer with a `> 来源: get_quote / get_news` block is grounded."""
    from tradingagents.agent_harness.verification.citations import citation_score
    answer = """
    ## 数据事实
    600036.SS 收 41.78, 涨 1.04%。
    ## 来源
    > get_quote (600036.SS) · get_news (600036.SS)
    """
    score = citation_score(answer, expected_sources=["get_quote", "get_news"])
    assert score >= 0.8


def test_citation_detector_flags_missing_sources():
    """An answer without a citation block is down-weighted."""
    from tradingagents.agent_harness.verification.citations import citation_score
    answer = "## 数据事实\n股价 41.78 元。\n## 方向性建议\n观望。"
    score = citation_score(answer, expected_sources=["get_quote"])
    assert score < 0.8


def test_citation_detector_partial_match():
    """An answer that cites some but not all expected tools gets a
    proportional mark — not 0, not 1."""
    from tradingagents.agent_harness.verification.citations import citation_score
    answer = "## 数据\n42 元\n\n> 来源: get_quote"
    score = citation_score(answer, expected_sources=["get_quote", "get_news"])
    assert 0.4 < score < 1.0
