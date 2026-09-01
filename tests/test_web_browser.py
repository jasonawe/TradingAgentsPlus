import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_server(url):
    import urllib.request

    for _ in range(100):
        try:
            with urllib.request.urlopen(url, timeout=0.2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"local browser server did not start: {url}")


def test_real_browser_navigation_paging_timeout_and_quote_refresh(tmp_path):
    if not os.environ.get("TRADINGAGENTS_PLAYWRIGHT"):
        pytest.skip("set TRADINGAGENTS_PLAYWRIGHT=1 to run the browser smoke harness")
    playwright = pytest.importorskip("playwright.sync_api")
    port = _free_port()
    env = os.environ.copy()
    env["TRADINGAGENTS_RESULTS_DIR"] = str(tmp_path)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "web.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    quote_requests = []
    history_pages = []
    try:
        _wait_for_server(f"{base_url}/api/config")
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            page = browser.new_page()

            def api_route(route):
                request = route.request
                parsed = urlparse(request.url)
                path = parsed.path
                if path == "/api/config":
                    route.fulfill(
                        json={
                            "supported_asset_types": ["stock", "crypto"],
                            "analyst_options": [
                                {"key": "market", "label": "Market Analyst", "label_key": "analysts.market"}
                            ],
                            "research_depths": [1, 3, 5],
                            "default_date": "2026-09-01",
                            "output_languages": [{"value": "Chinese", "label": "Chinese"}],
                            "output_language": "Chinese",
                            "providers": [
                                {
                                    "value": "openai",
                                    "label": "OpenAI",
                                    "quick_models": [{"value": "gpt-test", "label": "gpt-test"}],
                                    "deep_models": [{"value": "gpt-test", "label": "gpt-test"}],
                                }
                            ],
                            "configured": {
                                "provider": "openai",
                                "quick_model": "gpt-test",
                                "deep_model": "gpt-test",
                                "output_language": "Chinese",
                            },
                        }
                    )
                elif path == "/api/watchlist":
                    route.fulfill(json={"watchlist": {"version": 1}, "items": [{"id": "w1", "symbol": "600999.SS", "asset_type": "stock"}]})
                elif path == "/api/quotes":
                    quote_requests.append(request.url)
                    route.fulfill(
                        json={
                            "items": [
                                {
                                    "symbol": "600999.SS",
                                    "asset_type": "stock",
                                    "price": 18.7,
                                    "currency": "CNY",
                                    "asset_name_zh": "招商证券",
                                    "asset_name": "China Merchants Securities Co., Ltd.",
                                    "exchange_name_zh": "上海证券交易所",
                                    "exchange": "SHH",
                                    "source": "test",
                                    "freshness": "fresh",
                                    "cache_status": "live",
                                    "fetched_at": "2026-09-01T09:30:00Z",
                                }
                            ]
                        }
                    )
                elif path == "/api/history":
                    if "page" not in parse_qs(parsed.query):
                        route.fulfill(
                            json=[
                                {
                                    "report_id": "report-latest",
                                    "run_id": "run-latest",
                                    "ticker": "600999.SS",
                                    "analysis_date": "2026-09-01",
                                    "asset_type": "stock",
                                    "status": "completed",
                                    "rating": "Hold",
                                    "decision_preview": "最近一次分析摘要",
                                }
                            ]
                        )
                        return
                    page_number = int(parse_qs(parsed.query).get("page", ["1"])[0])
                    history_pages.append(page_number)
                    route.fulfill(
                        json={
                            "items": [
                                {
                                    "report_id": f"report-{page_number}",
                                    "run_id": f"run-{page_number}",
                                    "ticker": "600999.SS",
                                    "analysis_date": "2026-09-01",
                                    "asset_type": "stock",
                                    "status": "completed",
                                    "rating": "Hold",
                                    "decision_preview": f"第 {page_number} 页报告",
                                }
                            ],
                            "page": page_number,
                            "page_size": 20,
                            "total": 21,
                            "has_next": page_number == 1,
                        }
                    )
                elif path == "/api/runs/active":
                    route.fulfill(json={"run": None})
                elif path == "/api/runs" and request.method == "POST":
                    route.fulfill(
                        status=202,
                        json={
                            "run_id": "run-timeout",
                            "status": "running",
                            "progress": 0.2,
                            "started_at": "2026-09-01T09:30:00Z",
                            "request": {
                                "ticker": "600999.SS",
                                "analysis_date": "2026-09-01",
                                "asset_type": "stock",
                                "analysts": ["market"],
                                "research_depth": 1,
                                "provider": "openai",
                                "quick_model": "gpt-test",
                                "deep_model": "gpt-test",
                                "output_language": "Chinese",
                            },
                        },
                    )
                elif path == "/api/runs/run-timeout/events":
                    route.fulfill(
                        content_type="text/event-stream",
                        body='event: run_timed_out\ndata: {"run_id":"run-timeout","seq":1,"event":"run_timed_out","timestamp":"2026-09-01T09:31:00Z","payload":{"status":"timed_out","progress":0.2,"terminal_reason":"deadline_exceeded","error_message":"timeout"}}\n\n',
                    )
                elif path == "/api/runs/run-timeout":
                    route.fulfill(json={"run_id": "run-timeout", "status": "timed_out", "progress": 0.2, "request": {"ticker": "600999.SS"}})
                elif path == "/api/settings":
                    route.fulfill(json={"fields": {}})
                elif path == "/api/providers/market-data":
                    route.fulfill(json={"providers": []})
                else:
                    route.continue_()

            page.route("**/api/**", api_route)
            page.goto(base_url)
            page.locator(".asset-identity").get_by_text(
                "资产名称：招商证券", exact=True
            ).wait_for(timeout=5000)
            page.locator('[data-view="active"]').click()
            page.wait_for_url(f"{base_url}/active")
            assert page.locator("#active-empty").is_visible()
            assert page.locator("#run-header").is_hidden()
            assert page.locator("#run-grid").is_hidden()

            page.locator('[data-view="setup"]').click()
            page.wait_for_url(f"{base_url}/")
            initial_quotes = len(quote_requests)
            page.locator("#refresh-quotes").click()
            page.wait_for_timeout(200)
            assert len(quote_requests) > initial_quotes

            page.locator('[data-view="library"]').click()
            page.wait_for_url(f"{base_url}/reports")
            page.locator("#library-next").click()
            page.get_by_text("第 2 页报告", exact=True).wait_for(timeout=5000)
            assert 2 in history_pages

            page.locator('[data-view="analysis"]').click()
            page.locator("#ticker").fill("600999.SS")
            page.locator("#analysis-form button[type=submit]").click()
            page.locator("#terminal-panel h3").get_by_text(
                "分析超时", exact=True
            ).wait_for(timeout=5000)
            assert page.url == f"{base_url}/active"
            browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
