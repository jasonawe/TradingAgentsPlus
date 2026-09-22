"""§P3-5 — extract_prior_context + render_* helpers.

These helpers turn a prior report record into a compact payload that
gets injected into every analyst prompt and prepended to the new
run's ``complete_report.md``. The tests below lock in:

1. Normal extraction: signal, rating, summary from a typical report.
2. Summary heuristic: "核心观点"/"结论"/"Recommendation" headings
   win over arbitrary body text.
3. Empty / missing report → empty dict (graceful, never raises).
4. render_context_for_prompt produces a non-empty string with the
   key signal/rating fields and the "本次分析必须显式说明差异"
   directive.
5. render_context_for_report produces a markdown table that the
   reporting layer can prepend verbatim.
"""
from __future__ import annotations

import pytest

from tradingagents.agents.utils.prior_report_context import (
    extract_prior_context,
    render_context_for_prompt,
    render_context_for_report,
)


class _FakeHistory:
    """Duck-typed ``ReportHistory.get_report`` for tests."""

    def __init__(self, record: dict | None, raise_exc: Exception | None = None) -> None:
        self._record = record
        self._raise = raise_exc

    def get_report(self, report_id: str) -> dict:
        if self._raise is not None:
            raise self._raise
        if self._record is None:
            raise RuntimeError("not found")
        return self._record


_FULL_RECORD = {
    "report_id": "run-abc123",
    "ticker": "600036.SS",
    "analysis_date": "2026-09-02",
    "generated_at": "2026-09-02T04:49:19.369975+00:00",
    "signal": "Overweight",
    "rating": "Overweight",
    "status": "completed",
    "asset_type": "stock",
    "complete_report": (
        "# 招商银行 (600036.SS) 技术分析报告\n\n"
        "## 核心结论\n\n"
        "维持 **Overweight** 评级。当前股价 41 元，对应 2026E P/B 1.1x。\n\n"
        "## 1. 价格趋势分析\n\n"
        "从 2026-07-01 的 35.45 元低点起涨...\n"
    ),
}


def test_extract_normal_record_returns_structured_context():
    ctx = extract_prior_context("run-abc123", _FakeHistory(_FULL_RECORD))
    assert ctx["report_id"] == "run-abc123"
    assert ctx["ticker"] == "600036.SS"
    assert ctx["source_date"] == "2026-09-02"
    assert ctx["signal"] == "Overweight"
    assert ctx["rating"] == "Overweight"
    assert "维持 **Overweight**" in ctx["summary"]
    assert "从 2026-07-01" in ctx["raw_excerpt"]


def test_extract_picks_核心结论_summary_section():
    """The summary heuristic must pick the first heading matching
    the configured patterns — not the title or a random body line."""
    ctx = extract_prior_context("run-abc", _FakeHistory(_FULL_RECORD))
    # Summary should NOT include the title or the body section.
    assert "技术分析报告" not in ctx["summary"]
    assert "价格趋势分析" not in ctx["summary"]
    assert "维持 **Overweight**" in ctx["summary"]


def test_extract_handles_english_summary_heading():
    record = dict(_FULL_RECORD)
    record["complete_report"] = (
        "# TSLA Analysis\n\n"
        "## Executive Summary\n\n"
        "Maintain HOLD. Q3 deliveries miss guidance.\n\n"
        "## Detailed Analysis\n\n"
        "Long body...\n"
    )
    ctx = extract_prior_context("x", _FakeHistory(record))
    assert "Maintain HOLD" in ctx["summary"]
    assert "Long body" not in ctx["summary"]


def test_extract_falls_back_to_first_paragraph_when_no_heading_match():
    record = dict(_FULL_RECORD)
    record["complete_report"] = "Just a single paragraph with no heading."
    ctx = extract_prior_context("x", _FakeHistory(record))
    assert "single paragraph" in ctx["summary"]


def test_extract_returns_empty_dict_on_missing_report():
    """Never raises — a missing prior must not abort the run."""
    assert extract_prior_context("nope", _FakeHistory(None)) == {}
    assert extract_prior_context("nope", _FakeHistory(None, raise_exc=RuntimeError("boom"))) == {}


def test_extract_returns_partial_context_when_body_missing():
    """Signal/rating come from metadata, not the body — a
    complete_report-less record still produces a useful
    prior_context (signal + rating + empty summary)."""
    record = dict(_FULL_RECORD)
    record["complete_report"] = ""
    ctx = extract_prior_context("x", _FakeHistory(record))
    assert ctx["signal"] == "Overweight"
    assert ctx["summary"] == ""


def test_render_for_prompt_contains_key_fields_and_directive():
    ctx = extract_prior_context("run-abc", _FakeHistory(_FULL_RECORD))
    text = render_context_for_prompt(ctx)
    assert "前次分析参考" in text
    assert "Overweight" in text
    assert "600036.SS" in text
    assert "本次分析必须显式说明" in text and "差异" in text
    # The directive forces the LLM to compare, not just see prior.
    assert "本次分析必须显式说明" in text
    assert "差异" in text
    assert "本次与前次结论一致" in text


def test_render_for_prompt_empty_only_when_no_prior():
    """Only the empty dict means "no prior report"; a partial dict
    (just a signal, no metadata) is enough to render a comparison
    block — the user explicitly anchored the run on something."""
    assert render_context_for_prompt({}) == ""
    text = render_context_for_prompt({"signal": "Buy"})
    assert "Buy" in text
    assert "前次分析参考" in text


def test_render_for_report_produces_markdown_section():
    ctx = extract_prior_context("run-abc", _FakeHistory(_FULL_RECORD))
    md = render_context_for_report(ctx)
    assert md.startswith("## 对比前次")
    assert "| 信号 | Overweight |" in md
    assert "| 评级 | Overweight |" in md
    assert "run-abc" in md
    assert "前次核心观点" in md


def test_render_for_report_empty_when_no_prior():
    assert render_context_for_report({}) == ""
