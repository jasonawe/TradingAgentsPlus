from pathlib import Path

STATIC = Path(__file__).parents[1] / "web" / "static"


def test_static_console_assets_exist_and_are_self_contained():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '/static/app.js?v=' in html
    assert '/static/i18n.js?v=' in html
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


def test_investment_ratings_use_shared_localized_formatter_at_display_boundaries():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    rating_script = '/static/rating-labels.js?v='
    app_script = '/static/app.js?v='
    assert rating_script in html
    assert html.index(rating_script) < html.index(app_script)
    assert 'function formatRating(value) { return window.formatInvestmentRating(value, state.language); }' in js

    assert "const signal = formatRating(payload.signal);" in js
    assert 'signal ? t("run.signal", { signal }) : t("run.reportGenerated")' in js
    assert 'formatRating(report.rating || report.signal)' in js
    assert 'formatRating(record.rating || record.signal)' in js
    assert 'formatRating(analysis.rating || analysis.signal)' in js
    assert "signalLabel" not in js

    assert '<p>${escapeHtml(summary)}</p>' in js
    assert '<dd>${escapeHtml(value)}</dd>' in js
    assert '<span class="history-signal">${escapeHtml(status)}</span>' in js
    assert 'formatRating(analysis.rating || analysis.signal) || t("status.completed")' in js

    assert "renderReportMarkdown(formatRating" not in js
    assert "formatRating(report.executive_summary" not in js
    assert "formatRating(markdown" not in js
    assert "formatRating(report.complete_report" not in js
    assert "formatRating(report.report_id" not in js
    assert 'window.location.href = `/api/history/${encodeURIComponent(report.report_id)}/download`' in js


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


def test_primary_views_use_compact_headers_without_redundant_page_intros():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")

    for heading_id in (
        "setup-title",
        "analysis-page-title",
        "library-title",
        "settings-title",
        "active-title",
    ):
        assert f'id="{heading_id}" class="visually-hidden"' in html

    assert "analysis-page-header" not in html
    assert "active-page-header" not in html
    assert "view-subtitle" not in html
    assert 'class="compact-page-actions"' in html
    assert 'class="section-actions"' in html
    assert ".visually-hidden" in css
    assert ".form-title" in css and "font-size:20px" in css
    assert ".watchlist-panel h2 { margin:0; font-family:Georgia,serif; font-size:20px" in css
    assert ".watchlist-panel .section-heading h2 { font-size:20px; }" in css
    assert ".settings-card h2 { margin:0 0 18px; font-family:Georgia,serif; font-weight:400; font-size:20px" in css
    assert ".run-header h2" in css and "font-size:24px" in css
    assert ".report-heading h2" in css and "font-size:24px" in css
    assert '/static/styles.css?v=20260901-restore-route-fix-1' in html


def test_active_view_hidden_states_override_layout_display_rules():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")

    assert 'id="run-header" class="run-header" hidden' in html
    assert 'id="run-grid" class="run-grid" hidden' in html
    assert '[hidden] { display:none !important; }' in css


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
    resources = (STATIC / "i18n.js").read_text(encoding="utf-8")
    assert "i18n.assetIdentity(quote)" in js
    assert "asset_name_zh" in resources
    assert "exchange_name_zh" in resources
    assert "资产名称" in resources
    assert "交易市场" in resources
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
    assert '/static/quote-refresh.js?v=' in html
    assert "QuoteRefreshController" in js
    assert "aria-busy" in html + js


def test_quote_refresh_controller_and_freshness_labels_are_wired():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    controller = (STATIC / "quote-refresh.js").read_text(encoding="utf-8")
    assert "AbortController" in controller
    assert "4000" in controller
    assert "[5000, 10000, 20000, 40000, 60000]" in controller
    assert "sequence" in controller
    resources = (STATIC / "i18n.js").read_text(encoding="utf-8")
    for label in ("实时", "延迟", "缓存", "已过期", "不可用"):
        assert label in resources
    assert html.index("quote-refresh.js") < html.index("app.js")


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
    resources = (STATIC / "i18n.js").read_text(encoding="utf-8")
    assert "const I18N" not in js
    assert "window.TradingAgentsI18n" in js
    assert 'locale: "zh-CN"' in resources
    assert html.index("i18n.js") < html.index("app.js")
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
    assert '"run_timed_out"' in js
    assert "ACTIVE_RUN_STATUSES" in js
    assert 'switchView("active")' in js
    assert 'switchView("report")' in js
    assert 'switchView("active")' in js


def test_client_snapshot_replay_and_future_terminal_status_fallback():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "payload.snapshot_seq" in js
    assert "state.lastSeq = Number(payload.snapshot_seq)" in js
    assert "!ACTIVE_RUN_STATUSES.has(record.status)" in js
    assert 'new Set(["queued", "running", "publishing"])' in js
    assert '["queued", "running"].includes(active.status)' not in js
    assert '["queued", "running"].includes(record.status)' not in js
    assert "ACTIVE_RUN_STATUSES.has(active.status)" in js
    assert "ACTIVE_RUN_STATUSES.has(record.status)" in js
    assert 'case "run_timed_out"' in js
    assert '"error.timedOut": "分析超时"' in (STATIC / "i18n.js").read_text(encoding="utf-8")


def test_report_library_uses_server_pagination_and_request_sequencing():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for token in ("library-prev", "library-next", "library-total"):
        assert f'id="{token}"' in html
    for token in ("pageSize", "hasNext", "requestSeq", "loadLibraryPage", "URLSearchParams"):
        assert token in js
    assert "response.items || response" in js
    assert "state.library.page = 1" in js
    assert "client must not re-filter" not in js
