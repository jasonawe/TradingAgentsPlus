from pathlib import Path


STATIC = Path(__file__).parents[1] / "web" / "static"


def test_static_console_assets_exist_and_are_self_contained():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '<script src="/static/app.js" defer></script>' in html
    assert '<link rel="stylesheet" href="/static/styles.css" />' in html
    assert "https://" not in html + css + js
    assert 'id="analysis-form"' in html
    assert 'id="phase-timeline"' in html
    assert 'id="report-content"' in html


def test_client_contract_covers_api_events_reconnect_and_safe_rendering():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for token in ("/api/config", "/api/history", "/api/runs", "EventSource", "after_seq", "lastSeq", "seen", "run_completed", "run_failed", "run_cancelled"):
        assert token in js
    assert "escapeHtml" in js
    assert "REPORT_GROUPS" in js
    assert "fundamentals" in js


def test_css_has_narrow_viewport_layout_and_stable_activity_regions():
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "@media (max-width:760px)" in css
    assert ".activity-feed { height:390px" in css
    assert ".run-grid { display:grid" in css
