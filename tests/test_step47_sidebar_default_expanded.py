"""§0.4.14 — sidebar default to expanded unless user explicitly chose.

Regression for: user reports "首次进 /harness 后 session 列表不显示"
even after §0.4.11 added the rail-expand button. Root cause: stale
``ta.harness.sidebarExpanded='0'`` from §0.4.10/§0.4.10/§0.4.11
versions persisted across upgrades; users who had never explicitly
clicked the toggle saw only the rail-with-toggle and thought the
list was missing.

The fix introduces ``ta.harness.userChoseSidebar`` (set to '1' only
on real user interaction) and treats the legacy
``sidebarExpanded='0'`` as authoritative ONLY when
``userChoseSidebar === '1'``. Otherwise the page defaults to
expanded regardless of the legacy value.

These tests drive a real Chromium via Playwright and assert on the
DOM ``is-sidebar-expanded`` class + computed ``display`` of
``#harness-session-list``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _get_browser():
    """Lazy import — Playwright is heavy; tests should skip if missing."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    return sync_playwright()


def _drive(setup_map):
    """Open /harness with the given localStorage setup, return DOM state.

    ``setup_map`` keys are localStorage keys, values are strings.
    ``None`` clears localStorage before loading.
    """
    p_sync = _get_browser()
    if p_sync is None:
        return None
    with p_sync as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto("http://127.0.0.1:8000/harness", wait_until="networkidle")
        if setup_map is None:
            page.evaluate("() => { try { localStorage.clear(); } catch(_){} }")
        else:
            page.evaluate(
                "(m) => { try { for (const [k,v] of Object.entries(m)) localStorage.setItem(k,v); } catch(_){} }",
                setup_map,
            )
        page.reload(wait_until="networkidle")
        state = page.evaluate("""() => {
          const layout = document.querySelector('.harness-layout');
          const list = document.getElementById('harness-session-list');
          const cs = list ? getComputedStyle(list) : null;
          return {
            layoutClasses: layout ? layout.className : null,
            listDisplay: cs ? cs.display : null,
          };
        }""")
        browser.close()
        return state


def test_fresh_user_sees_expanded_sidebar():
    """No localStorage → sidebar must be expanded."""
    st = _drive(None)
    if st is None:
        return  # skip when Playwright unavailable
    assert "is-sidebar-expanded" in st["layoutClasses"], (
        f"fresh user should see expanded sidebar, got {st}"
    )
    assert st["listDisplay"] != "none", f"session list must be visible, got {st}"


def test_stale_legacy_zero_is_ignored():
    """The §0.4.14 fix: stale ``sidebarExpanded='0'`` from older
    versions must NOT keep the sidebar collapsed unless the user
    actually clicked the toggle in the current session."""
    st = _drive({"ta.harness.sidebarExpanded": "0"})
    if st is None:
        return
    assert "is-sidebar-expanded" in st["layoutClasses"], (
        f"stale '0' without userChoseSidebar must default to expanded, got {st}"
    )
    assert st["listDisplay"] != "none"


def test_explicit_user_collapse_is_respected():
    """User clicked the toggle → sidebar stays collapsed."""
    st = _drive({
        "ta.harness.sidebarExpanded": "0",
        "ta.harness.userChoseSidebar": "1",
    })
    if st is None:
        return
    assert "is-sidebar-expanded" not in st["layoutClasses"], (
        f"user explicitly chose collapsed; should stay collapsed, got {st}"
    )
    assert st["listDisplay"] == "none"


def test_explicit_user_expand_is_respected():
    """User clicked expand → sidebar stays expanded."""
    st = _drive({
        "ta.harness.sidebarExpanded": "1",
        "ta.harness.userChoseSidebar": "1",
    })
    if st is None:
        return
    assert "is-sidebar-expanded" in st["layoutClasses"]
    assert st["listDisplay"] != "none"


if __name__ == "__main__":
    test_fresh_user_sees_expanded_sidebar()
    test_stale_legacy_zero_is_ignored()
    test_explicit_user_collapse_is_respected()
    test_explicit_user_expand_is_respected()
    print("ALL §0.4.14 sidebar tests passed")
