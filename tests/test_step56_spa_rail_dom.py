"""§0.4.21 — SPA /harness view ships rail containers so users can
collapse the sidebar in the in-app routing too (not only on the
standalone harness.html route)."""
import re
from pathlib import Path

INDEX = Path("web/static/index.html").read_text()
HARNESS_JS = Path("web/static/harness.js").read_text()


def test_index_html_harness_view_has_toggle():
    """The SPA harness view must include the sidebar-toggle button so
    init()'s toggle handler has a target to bind to."""
    # extract the #harness-view section
    m = re.search(
        r'<section id="harness-view"[^>]*>(.*?)</section>',
        INDEX,
        re.S,
    )
    assert m, "could not locate #harness-view section"
    section = m.group(1)
    assert 'id="harness-sidebar-toggle"' in section, (
        "harness-sidebar-toggle missing in SPA /harness view"
    )


def test_index_html_harness_view_has_rail():
    """SPA harness view must include the rail container + new/expand
    buttons so users can collapse the sidebar to rail mode."""
    m = re.search(
        r'<section id="harness-view"[^>]*>(.*?)</section>',
        INDEX,
        re.S,
    )
    section = m.group(1)
    for needed in [
        'id="harness-rail-new"',
        'id="harness-rail-expand"',
        'id="harness-rail-count"',
        'id="harness-rail-active-title"',
        'class="harness-sidebar-rail"',
    ]:
        assert needed in section, f"missing {needed}"


def test_index_html_has_rail_icon_symbols():
    """The icons used by the rail buttons (i-plus, i-menu) must exist
    in the SVG sprite."""
    assert '<symbol id="i-plus"' in INDEX
    assert '<symbol id="i-menu"' in INDEX


def test_session_list_rendered_after_reconcile():
    """(regression) reconcileActiveSession still calls loadSessions."""
    assert "await loadSessions();" in HARNESS_JS
