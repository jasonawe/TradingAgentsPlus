"""§P3-5 — extract a structured ``prior_context`` from an existing report.

When a user asks "再分析一下 600036 (基于昨天的报告)", the runner fetches
the prior report and turns it into a compact, structured payload
that:

1. Gets injected into every analyst's prompt as
   ``{prior_context}`` so the LLM can compare new findings vs prior.
2. Gets prepended to ``complete_report.md`` as a "对比前次" delta
   section so the user sees the prior→current transition at a glance.

Design notes
------------
* We deliberately **don't** ship the full prior ``complete_report``
  to the LLM (would explode prompt size). The LLM only sees the
  *compressed* context — signal, key numbers, prior conclusion.
* The context is rendered as plain text (no markdown) so it can be
  inserted into either a prompt template or a markdown report
  without re-parsing.
* Failures are graceful: a missing/corrupt prior report produces an
  empty context, never an exception — the run then behaves like a
  standalone analysis (no comparison injected). The runner logs the
  reason.
"""
from __future__ import annotations

import logging
import re
from typing import Any

LOGGER = logging.getLogger(__name__)


# Cap the body excerpts so a single huge prior report doesn't blow
# up the prompt budget. 1500 chars covers ~3 paragraphs of dense
# analysis; if the LLM needs more detail it can call ``get_report``.
_MAX_BODY_CHARS = 1500

# Headings we look for in the prior body. First match wins.
_SUMMARY_PATTERNS = [
    r"(?:核心观点|核心结论|投资观点|投资结论|投资建议|结论|Executive Summary|Summary|Recommendation)\b",
]


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    s = s.strip()
    if len(s) <= n:
        return s
    return s[: n - 30].rstrip() + "\n... (前次报告摘要已截断)"


def _extract_summary(body: str) -> str:
    """Find the "main conclusion" section in the prior body.

    Heuristic: first H1/H2/H3/H4 matching any of the configured
    patterns, then take everything from there until the next heading
    of equal or higher level.
    """
    if not body:
        return ""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,4})\s+(.+)$", line.strip())
        if not m:
            continue
        level = len(m.group(1))
        heading = m.group(2)
        if not any(re.search(p, heading, re.IGNORECASE) for p in _SUMMARY_PATTERNS):
            continue
        # Collect until next heading of equal/higher level.
        buf: list[str] = []
        for follow in lines[i + 1:]:
            fm = re.match(r"^(#{1,4})\s+", follow)
            if fm and len(fm.group(1)) <= level:
                break
            buf.append(follow)
        return _truncate("\n".join(buf).strip(), _MAX_BODY_CHARS)
    # No structured "conclusion" header found — fall back to first
    # paragraph of the body.
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    return _truncate(paragraphs[0] if paragraphs else "", _MAX_BODY_CHARS)


def extract_prior_context(
    report_id: str,
    history: Any,
) -> dict[str, str]:
    """Turn a prior report record into a structured ``prior_context``.

    Parameters
    ----------
    report_id
        The ``report_id`` to look up (from ``list_reports``).
    history
        Anything with a ``get_report(report_id) -> dict`` method.
        In production this is ``web.history.ReportHistory``. We
        accept the duck-typed shape so unit tests can pass a fake.

    Returns
    -------
    dict with keys: ``report_id``, ``source_date``, ``ticker``,
    ``signal``, ``rating``, ``summary``, ``raw_excerpt``.

    On any failure (missing report, no body, malformed metadata),
    returns an empty dict — never raises. The caller can check
    ``if not prior_context:`` to skip the comparison path.
    """
    empty: dict[str, str] = {}
    try:
        record = history.get_report(report_id)
    except Exception as e:
        LOGGER.warning(
            "extract_prior_context: get_report(%s) failed: %s",
            report_id, e,
        )
        return empty
    if not isinstance(record, dict):
        return empty
    body = record.get("complete_report") or ""
    summary = _extract_summary(body)
    # Take a second excerpt (head of body) for the "raw" block —
    # gives the LLM some verbatim numbers to anchor on without
    # forcing it to fit the whole report into the prompt.
    raw_excerpt = _truncate(body, 800)
    return {
        "report_id": record.get("report_id") or report_id,
        "source_date": (
            record.get("analysis_date")
            or record.get("generated_at")
            or ""
        ),
        "ticker": record.get("ticker") or "",
        "signal": record.get("signal") or "",
        "rating": record.get("rating") or record.get("signal") or "",
        "summary": summary,
        "raw_excerpt": raw_excerpt,
    }


def render_context_for_prompt(prior_context: dict[str, str]) -> str:
    """Format ``prior_context`` as plain text for an LLM prompt.

    Returns ``""`` when the dict is empty (no prior report) so the
    prompt template can just call this and ignore the result when
    empty — analysts only see the comparison block when it exists.
    """
    if not prior_context:
        return ""
    lines = [
        "【前次分析参考】",
        f"- 报告 ID: {prior_context.get('report_id', '?')}",
        f"- 分析日期: {prior_context.get('source_date', '?')}",
        f"- 标的: {prior_context.get('ticker', '?')}",
    ]
    sig = prior_context.get("signal") or ""
    if sig:
        lines.append(f"- 当时信号: {sig}")
    rating = prior_context.get("rating") or ""
    if rating and rating != sig:
        lines.append(f"- 当时评级: {rating}")
    summary = prior_context.get("summary") or ""
    if summary:
        lines.append("")
        lines.append("前次核心观点:")
        lines.append(summary)
    lines.append("")
    lines.append(
        "【重要】本次分析必须显式说明与前次的差异：哪些结论被证实、"
        "哪些被推翻、哪些出现了新变量。如果没有差异，"
        "也必须写明\"本次与前次结论一致 + 一致的原因\"。"
    )
    return "\n".join(lines)


def render_context_for_report(prior_context: dict[str, str]) -> str:
    """Format ``prior_context`` as a markdown section for the report header.

    Returns ``""`` when empty so the report omits the section entirely.
    """
    if not prior_context:
        return ""
    parts = [
        "## 对比前次",
        "",
        f"本次分析与 **{prior_context.get('report_id', '?')}** "
        f"({prior_context.get('source_date', '?')}) 的结论对比：",
        "",
        "| 字段 | 前次 |",
        "|---|---|",
        f"| 信号 | {prior_context.get('signal', '—')} |",
        f"| 评级 | {prior_context.get('rating', '—')} |",
        "",
    ]
    summary = prior_context.get("summary") or ""
    if summary:
        parts.append("**前次核心观点**:")
        parts.append("")
        parts.append(summary)
        parts.append("")
    return "\n".join(parts)
