import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke


def test_browser_harness_is_available_or_skipped():
    """Keep a deterministic opt-in Playwright harness for local smoke testing."""
    if not os.environ.get("TRADINGAGENTS_PLAYWRIGHT"):
        pytest.skip("set TRADINGAGENTS_PLAYWRIGHT=1 to run the browser smoke harness")
    playwright = pytest.importorskip("playwright.sync_api")
    assert playwright is not None


def test_browser_flow_contract_is_deterministic_and_local():
    """The opt-in browser suite must exercise the real local console workflow."""
    static = Path(__file__).parents[1] / "web" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    for token in (
        "analysis-view",
        "analysis-form",
        "watchlist-list",
        "library-list",
        "phase-timeline",
        "activity-feed",
        "cancel-run",
        "report-content",
    ):
        assert token in html
    for token in ("EventSource", "run_cancelled", "run_failed", "run_completed", "/api/history/", "download"):
        assert token in js
    assert "after_seq" in js and "seen" in js
