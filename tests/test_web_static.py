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


def test_css_has_narrow_viewport_layout_and_stable_activity_regions():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "@media (max-width:760px)" in css
    assert ".activity-feed { height:390px" in css
    assert ".run-grid { display:grid" in css


def test_markdown_report_supports_tables_and_compact_setup_title():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "table-wrap" in css
    assert "<table>" in js
    assert ".intro-grid h1" in css
    assert "font-size:clamp(32px,4vw,52px)" in css


def test_client_restores_an_active_run_after_page_reload():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/api/runs/active" in js
    assert "restoreActiveRun" in js
    assert "tradingagents-active-run" in js


def test_completed_report_hides_live_research_desk_panel():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="run-grid"' in html
    assert '$("run-grid").hidden = true' in js
    assert '$("run-grid").hidden = false' in js
    assert 'function renderReport(report) { $("run-grid").hidden = true;' in js
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
    assert 'id="settings-view"' in html
    assert '"run_interrupted"' in js
    assert '"completed", "interrupted", "failed", "cancelled"' in js
    assert '$("run-header").hidden = true' in js
