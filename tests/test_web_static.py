from pathlib import Path

STATIC = Path(__file__).parents[1] / "web" / "static"


def test_static_console_assets_exist_and_are_self_contained():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '/static/app.js?v=' in html
    assert '/static/styles.css?v=' in html
    assert "https://" not in html + css + js
    assert 'id="analysis-form"' in html
    assert 'id="phase-timeline"' in html
    assert 'id="report-content"' in html


def test_client_contract_covers_api_events_reconnect_and_safe_rendering():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for token in ("/api/config", "/api/history", "/api/runs", "/api/watchlist", "/api/quotes", "EventSource", "after_seq", "lastSeq", "seen", "run_completed", "run_failed", "run_cancelled", "run_interrupted", "reconnectStatus", "syncRecord", "showSettings"):
        assert token in js
    assert "escapeHtml" in js
    assert "REPORT_GROUPS" in js
    assert "fundamentals" in js
    assert "report-meta" in (STATIC / "index.html").read_text(encoding="utf-8")
    assert "elapsed-time" in (STATIC / "index.html").read_text(encoding="utf-8")
    assert "back-history" in (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="executive-summary"' in (STATIC / "index.html").read_text(encoding="utf-8")
    assert "executive_summary" in js
    assert "complete_report_html" in js
    assert "executive_summary_html" in js


def test_css_has_narrow_viewport_layout_and_stable_activity_regions():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "@media (max-width:760px)" in css
    assert ".activity-feed { height:390px" in css
    assert ".run-grid { display:grid" in css


def test_css_uses_fluid_container_and_intermediate_responsive_breakpoints():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "width:min(1440px, calc(100% - clamp(28px, 6vw, 96px)))" in css
    assert "@media (max-width:1080px)" in css
    assert "@media (max-width:920px)" in css
    assert "@media (max-width:760px)" in css
    assert ".watchlist-row { grid-template-columns:1fr 1fr" in css
    assert ".library-toolbar { grid-template-columns:1fr 1fr" in css


def test_markdown_report_supports_tables_and_watchlist_uses_compact_rows():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "table-wrap" in css
    assert 'querySelectorAll("table")' in js
    assert "complete_report_html" in js
    assert ".watchlist-row" in css
    assert ".asset-identity" in css
    assert "watchlist-analysis" in js
    assert "latestAnalysisFor" in js
    assert "asset_name" in js
    assert "exchange" in js
    assert "资产名称" in js
    assert "交易市场" in js
    assert "asset_name_zh" in js
    assert "exchange_name_zh" in js
    assert "cleanSummary" in js


def test_watchlist_is_separate_from_analysis_and_refreshes_quotes_periodically():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    setup_start = html.index('id="setup-view"')
    analysis_start = html.index('id="analysis-view"')
    form_start = html.index('id="analysis-form"')
    assert 'data-view="analysis"' in html
    assert "先明确问题" not in html
    assert "让研究台展开调查" not in html
    assert setup_start < analysis_start < form_start
    setup_fragment = html[setup_start:analysis_start]
    assert 'id="analysis-form"' not in setup_fragment
    assert 'class="history-section"' not in setup_fragment
    assert "QUOTE_REFRESH_MS = 5000" in js
    assert "setInterval" in js
    assert "quoteRefreshTimer" in js
    assert "aria-busy" in html + js


def test_client_restores_an_active_run_after_page_reload():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/api/runs/active" in js
    assert "restoreActiveRun" in js
    assert "tradingagents-active-run" in js


def test_client_uses_persisted_progress_and_keeps_market_identity_readable():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "syncRunSnapshot" in js
    assert "record?.progress" in js
    assert "record?.current_agent" in js
    identity_start = css.index(".watchlist-asset .asset-identity")
    identity_rule = css[identity_start:css.index("}", identity_start)]
    assert "overflow:visible" in identity_rule
    assert "white-space:normal" in identity_rule
    assert "overflow-wrap:anywhere" in identity_rule
    assert "text-overflow:ellipsis" not in identity_rule


def test_client_routes_views_and_handles_browser_history():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for path in ("/analysis", "/active", "/reports", "/settings", "/reports/"):
        assert path in js
    assert "pushState" in js
    assert "replaceState" in js
    assert "popstate" in js
    assert "applyRoute" in js
    assert 'data-view="analysis"' in html


def test_completed_report_hides_live_research_desk_panel():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="run-grid"' in html
    assert '$("run-grid").hidden = true' in js
    assert '$("run-grid").hidden = false' in js
    assert 'function renderReport(report) { switchView("report");' in js
    assert '$("report-panel").hidden = false' in js
    assert 'status: "completed"' in js
    assert '/static/app.js?v=' in html


def test_client_uses_chinese_product_chrome_and_keeps_report_language_selection():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for token in (
        'lang="zh-CN"',
        'data-view="settings"',
        'id="watchlist-panel"',
        'id="settings-view"',
        "renderFormOptions",
        "output_language",
        "English",
        "分析师团队",
    ):
        assert token in html + js
    assert 'id="language-toggle"' not in html
    assert "en-US" not in html


def test_localized_client_does_not_leave_user_facing_literals_outside_dictionary():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const I18N" in js
    assert "applyTranslations" in js
    assert "data-i18n=" in html
    for token in ("Loading history...", "No briefings saved yet.", "Start analysis"):
        assert token not in html


def test_report_view_is_separate_from_live_progress_and_supports_interruptions():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="run-header"' in html
    assert 'data-view="active"' in html
    assert 'id="report-view"' in html
    run_start = html.index('id="active-view"')
    report_start = html.index('id="report-view"')
    assert 'id="report-panel"' not in html[run_start:report_start]
    assert 'id="report-panel"' in html[report_start:]
    assert 'id="report-back-library"' in html
    assert '$("report-back-library").addEventListener' in js
    assert 'id="settings-view"' in html
    assert '"run_interrupted"' in js
    assert '"completed", "interrupted", "failed", "cancelled"' in js
    assert 'switchView("active")' in js
    assert 'switchView("report")' in js
    assert 'switchView("active")' in js
