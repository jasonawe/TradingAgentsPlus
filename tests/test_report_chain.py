"""§P3-5 — chain walker for the "基于报告再分析" feature.

The walker lives in ``web.report_chain`` and is responsible for
turning a ``report_id`` into either a list of chain links (for the
lineage breadcrumb UI) or a single-element summary of the immediate
prior (for the "对比前次" panel at the top of the report detail page).

These tests pin down:

1. Normal chain (C -> B -> A -> root). Newest first, full lineage.
2. Standalone report (no prior link) -> empty chain / None prior.
3. Missing prior (claim a prior id but the prior is gone) -> walker
   stops gracefully and ``resolve_prior_summary`` returns a
   ``{"missing": True, "report_id": ...}`` marker so the UI can
   render "前次报告缺失".
4. Cycle (A -> B -> A) -> visited-set prevents infinite recursion.
5. max_depth cap (default 10) -> walker stops at the cap.
6. ``history.get_report`` raises (DB hiccup) -> returns None / empty
   rather than blowing up the request.
"""
from __future__ import annotations

import pytest

from web.report_chain import (
    resolve_prior_summary,
    walk_report_chain,
    _MAX_DEPTH,
)


class _FakeHistory:
    """Duck-typed ``ReportHistory.get_report`` for tests."""

    def __init__(self, entries: dict, errors: set | None = None) -> None:
        self._entries = entries
        self._errors = errors or set()
        self.calls: list[str] = []

    def get_report(self, report_id: str) -> dict:
        self.calls.append(report_id)
        if report_id in self._errors:
            raise RuntimeError(f"disk gone: {report_id}")
        rec = self._entries.get(report_id)
        if rec is None:
            raise RuntimeError(f"not found: {report_id}")
        return rec


def _rec(report_id: str, *, ticker: str, signal: str, prior: str | None) -> dict:
    return {
        "report_id": report_id,
        "ticker": ticker,
        "signal": signal,
        "rating": signal,
        "analysis_date": "2026-09-02",
        "generated_at": "2026-09-02T04:49:19.369975+00:00",
        "based_on_report_id": prior,
    }


class TestWalkReportChain:
    def test_normal_three_link_chain(self):
        entries = {
            "run-c": _rec("run-c", ticker="600036.SS", signal="Overweight", prior="run-b"),
            "run-b": _rec("run-b", ticker="600036.SS", signal="Hold", prior="run-a"),
            "run-a": _rec("run-a", ticker="600036.SS", signal="Sell", prior=None),
        }
        history = _FakeHistory(entries)
        chain = walk_report_chain(history, "run-c")
        assert [e["report_id"] for e in chain] == ["run-c", "run-b", "run-a"]
        assert chain[0]["based_on_report_id"] == "run-b"
        assert chain[-1]["based_on_report_id"] is None

    def test_standalone_returns_single_link(self):
        entries = {"run-x": _rec("run-x", ticker="NVDA", signal="Buy", prior=None)}
        history = _FakeHistory(entries)
        chain = walk_report_chain(history, "run-x")
        assert len(chain) == 1
        assert chain[0]["report_id"] == "run-x"
        assert chain[0]["based_on_report_id"] is None

    def test_missing_prior_stops_gracefully(self):
        entries = {
            "run-missing": _rec("run-missing", ticker="BTC-USD", signal="Buy", prior="run-ghost"),
        }
        history = _FakeHistory(entries)
        chain = walk_report_chain(history, "run-missing")
        assert len(chain) == 1
        assert chain[0]["report_id"] == "run-missing"

    def test_cycle_protection(self):
        entries = {
            "run-a": _rec("run-a", ticker="600036.SS", signal="Hold", prior="run-b"),
            "run-b": _rec("run-b", ticker="600036.SS", signal="Buy", prior="run-a"),
        }
        history = _FakeHistory(entries)
        chain = walk_report_chain(history, "run-a")
        ids = [e["report_id"] for e in chain]
        assert ids == ["run-a", "run-b"]
        assert "run-b" in history.calls

    def test_max_depth_cap(self):
        prior = None
        entries: dict[str, dict] = {}
        for i in range(15):
            rid = f"run-{i}"
            entries[rid] = _rec(rid, ticker="600036.SS", signal="Hold", prior=prior)
            prior = rid
        head = "run-14"
        history = _FakeHistory(entries)
        chain = walk_report_chain(history, head, max_depth=_MAX_DEPTH)
        assert len(chain) == _MAX_DEPTH
        assert chain[0]["report_id"] == head

    def test_history_get_report_raises_returns_empty(self):
        history = _FakeHistory({}, errors={"run-broken"})
        chain = walk_report_chain(history, "run-broken")
        assert chain == []


class TestResolvePriorSummary:
    def test_no_prior_returns_none(self):
        entries = {"run-x": _rec("run-x", ticker="NVDA", signal="Buy", prior=None)}
        history = _FakeHistory(entries)
        assert resolve_prior_summary(history, "run-x") is None

    def test_prior_missing_marks_missing_true(self):
        entries = {
            "run-current": _rec("run-current", ticker="600036.SS", signal="Buy", prior="run-ghost"),
        }
        history = _FakeHistory(entries)
        summary = resolve_prior_summary(history, "run-current")
        assert summary == {"report_id": "run-ghost", "missing": True}

    def test_prior_resolved_returns_full_summary(self):
        entries = {
            "run-current": _rec("run-current", ticker="600036.SS", signal="Buy", prior="run-prior"),
            "run-prior": {
                "report_id": "run-prior",
                "ticker": "600036.SS",
                "signal": "Hold",
                "rating": "Hold",
                "analysis_date": "2026-09-01",
                "generated_at": "2026-09-01T00:00:00+00:00",
                "based_on_report_id": None,
                "complete_report": "## 核心观点\n过去 30 天走势偏弱。\n\n## 详细分析\n...省略...",
            },
        }
        history = _FakeHistory(entries)
        summary = resolve_prior_summary(history, "run-current")
        assert summary is not None
        assert summary["missing"] is False
        assert summary["report_id"] == "run-prior"
        assert summary["signal"] == "Hold"
        assert summary["ticker"] == "600036.SS"
        assert "核心观点" in summary["summary"]

    def test_current_record_unresolvable_returns_none(self):
        history = _FakeHistory({}, errors={"run-ghost"})
        assert resolve_prior_summary(history, "run-ghost") is None
