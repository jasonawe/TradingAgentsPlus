import os

import pytest

pytestmark = pytest.mark.smoke


def test_browser_harness_is_available_or_skipped():
    """Keep a deterministic opt-in Playwright harness for local smoke testing."""
    if not os.environ.get("TRADINGAGENTS_PLAYWRIGHT"):
        pytest.skip("set TRADINGAGENTS_PLAYWRIGHT=1 to run the browser smoke harness")
    playwright = pytest.importorskip("playwright.sync_api")
    assert playwright is not None

