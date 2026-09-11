"""Day 7 — 6 个新 tool 对接 TradingAgentsPlus 核心能力集成测试。

覆盖:
- run_trading_agents_analysis:启动主图 + 同步返回 run_id(用 mock RunManager)
- get_analysis_status:从 RunManager 拿状态(用 mock)
- get_news:调 alpha_vantage_news.get_news
- list_scheduled_tasks / run_scheduled_task:ScheduledAnalysisService 的接口
- list_reports:ReportHistory 接口
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradingagents.agents.general import tools_bridge as tb


# ─────────────────────────────────────────────────────
# Mock 对象(模拟 web app 上下文里的实例)
# ─────────────────────────────────────────────────────


class _MockRunRecord:
    def __init__(self, run_id="run-mock-001"):
        self.run_id = run_id
        self.status = "completed"
        self.queued_at = "2026-09-11T10:00:00"
        self.started_at = "2026-09-11T10:00:01"
        self.finished_at = "2026-09-11T10:01:30"
        self.error = None

        class _Req:
            ticker = "600036.SS"

        self.request = _Req()


class _MockRunManager:
    def start_run(self, request, worker=None, *, run_id=None):
        # 校验 AnalysisRequest 字段
        assert request.ticker
        assert request.analysis_date
        assert request.research_depth in (1, 3, 5)
        return _MockRunRecord()

    def get_run(self, run_id):
        if run_id == "run-bad":
            raise KeyError("not found")
        return _MockRunRecord(run_id=run_id)


class _MockScheduler:
    def list_jobs(self):
        return {
            "jobs": [
                {"id": "job-1", "ticker": "600036.SS", "cron": "0 9 * * 1-5", "enabled": True},
                {"id": "job-2", "ticker": "BTC-USD", "cron": "0 0 * * *", "enabled": False},
            ]
        }

    def run_now(self, job_id):
        return {"status": "triggered", "job_id": job_id, "run_id": f"run-{job_id}-001"}


class _MockReportHistory:
    def list_reports(self):
        return [
            {
                "report_id": "r-001",
                "ticker": "600036.SS",
                "status": "completed",
                "started_at": "2026-09-10T10:00:00",
                "rating": "BUY",
            },
            {
                "report_id": "r-002",
                "ticker": "600000.SS",
                "status": "completed",
                "started_at": "2026-09-09T14:30:00",
                "rating": "HOLD",
            },
        ]


def _install_mocks():
    """手动注入 mock 对象,模拟 web app 启动时的 set_*() 调用。"""
    tb.set_active_runner(_MockRunManager())
    tb.set_scheduler_service(_MockScheduler())
    tb.set_news_provider(lambda ticker, s, e: f"[MOCK NEWS for {ticker}] {s} → {e}")
    tb.set_report_history(_MockReportHistory())


# ─────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────


def test_run_trading_agents_analysis_starts_pipeline():
    _install_mocks()
    result = tb.run_trading_agents_analysis.invoke(
        {"symbol": "600036.SS", "trade_date": "2026-09-11", "research_depth": 1}
    )
    assert "started" in result
    assert "600036.SS" in result
    assert "run-mock-001" in result
    print(f"  ✓ run_trading_agents_analysis: {result[:100]}")


def test_run_trading_agents_analysis_invalid_date():
    _install_mocks()
    result = tb.run_trading_agents_analysis.invoke(
        {"symbol": "600036.SS", "trade_date": "2026-13-99"}
    )
    assert "ERROR" in result
    assert "YYYY-MM-DD" in result
    print(f"  ✓ invalid date caught: {result[:100]}")


def test_run_trading_agents_analysis_invalid_asset_type():
    _install_mocks()
    result = tb.run_trading_agents_analysis.invoke(
        {"symbol": "BTC-USD", "asset_type": "forex"}
    )
    assert "ERROR" in result
    print(f"  ✓ invalid asset_type caught: {result[:100]}")


def test_get_analysis_status():
    _install_mocks()
    result = tb.get_analysis_status.invoke({"run_id": "run-abc123"})
    assert "completed" in result
    assert "600036.SS" in result
    print(f"  ✓ get_analysis_status: {result[:100]}")


def test_get_news_via_callable_provider():
    _install_mocks()
    result = tb.get_news.invoke({"symbol": "600036.SS", "days": 3})
    assert "MOCK NEWS" in result
    assert "600036.SS" in result
    print(f"  ✓ get_news: {result[:120]}")


def test_list_scheduled_tasks():
    _install_mocks()
    result = tb.list_scheduled_tasks.invoke({})
    assert "job-1" in result or "job-2" in result
    print(f"  ✓ list_scheduled_tasks: {result[:120]}")


def test_run_scheduled_task():
    _install_mocks()
    result = tb.run_scheduled_task.invoke({"job_id": "job-1"})
    assert "triggered" in result
    assert "job-1" in result
    print(f"  ✓ run_scheduled_task: {result[:120]}")


def test_list_reports_with_filter():
    _install_mocks()
    result = tb.list_reports.invoke({"symbol": "600036.SS", "limit": 5})
    assert "r-001" in result
    assert "r-002" not in result  # 不同 ticker 被过滤
    print(f"  ✓ list_reports (filter): {result[:120]}")


def test_list_reports_no_filter():
    _install_mocks()
    result = tb.list_reports.invoke({})
    assert "r-001" in result
    assert "r-002" in result
    print(f"  ✓ list_reports (no filter): {result[:120]}")


def test_all_tools_count():
    """确认 ALL_TOOLS 包含 6 个新 tool,总数 21。"""
    assert len(tb.ALL_TOOLS) == 21, f"expected 21, got {len(tb.ALL_TOOLS)}"
    names = {t.name for t in tb.ALL_TOOLS}
    expected_new = {
        "run_trading_agents_analysis", "get_analysis_status",
        "get_news", "list_scheduled_tasks", "run_scheduled_task",
        "list_reports",
    }
    missing = expected_new - names
    assert not missing, f"missing Day 7 tools: {missing}"
    print(f"  ✓ ALL_TOOLS contains 21 tools (15 + 6 new)")


# ─────────────────────────────────────────────────────
# Test runner
# ─────────────────────────────────────────────────────


if __name__ == "__main__":
    print("=" * 60)
    print("Day 7 — 6 new tools + TradingAgentsPlus integration test")
    print("=" * 60)

    tests = [
        test_all_tools_count,
        test_run_trading_agents_analysis_starts_pipeline,
        test_run_trading_agents_analysis_invalid_date,
        test_run_trading_agents_analysis_invalid_asset_type,
        test_get_analysis_status,
        test_get_news_via_callable_provider,
        test_list_scheduled_tasks,
        test_run_scheduled_task,
        test_list_reports_with_filter,
        test_list_reports_no_filter,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print()
    print("=" * 60)
    print(f"Day 7 tests: {passed} passed, {failed} failed")
    print("=" * 60)

    if failed:
        sys.exit(1)
