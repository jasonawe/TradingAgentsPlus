"""§0.4.19.fix — sidebar default-expanded policy.

History:
- §0.4.14 introduced userChoseSidebar flag.
- §0.4.19.fix (a) un-stuck legacy ``sidebarExpanded='0'`` values.
- §0.4.19.fix (b) lifted the expand decision out of the
  ``if (sidebarToggle && layout)`` gate so the SPA /harness view
  (which doesn't ship the toggle DOM) also lands the
  ``is-sidebar-expanded`` class on first visit.
"""
import re
from pathlib import Path

HARNESS_JS = Path("web/static/harness.js").read_text()


def test_set_sidebar_expanded_called_outside_toggle_gate():
    """The decision block + ``setSidebarExpanded`` call must NOT be
    nested inside an ``if (sidebarToggle && layout)`` gate — that gate
    is absent on the SPA /harness view, which omits the toggle DOM."""
    # Find the initialExpanded decision and ensure it's reachable
    # regardless of sidebarToggle presence.
    decision = re.search(
        r"let initialExpanded = true;.*?setSidebarExpanded\(initialExpanded, false\)",
        HARNESS_JS,
        re.S,
    )
    assert decision, "decision + setSidebarExpanded call not found"
    block = decision.group()
    # The decision must NOT contain the toggle-gate's signature.
    assert "if (sidebarToggle && layout) {" not in block, (
        "decision block must not be inside the toggle gate"
    )


def test_user_collapse_only_when_user_chose_explicit():
    """Returning users who clicked the toggle to collapse (``userChose='1'``
    + ``stored='0'``) must stay collapsed. Everyone else is expanded."""
    m = re.search(
        r"let initialExpanded = true;(.+?)setSidebarExpanded\(initialExpanded",
        HARNESS_JS,
        re.S,
    )
    assert m, "decision block not found"
    block = m.group(1)
    assert 'if (userChose === "1" && stored === "0")' in block
    assert "initialExpanded = false" in block


def test_toggle_handler_only_when_toggle_dom_exists():
    """The toggle click listener stays gated on sidebarToggle existence —
    the SPA /harness view doesn't ship a toggle button so binding would
    be a no-op anyway."""
    # After the gate move, we expect:
    #   setSidebarExpanded(...)
    #   if (sidebarToggle && layout) { sidebarToggle.addEventListener(...) }
    m = re.search(
        r"setSidebarExpanded\(initialExpanded, false\);(.+?)$",
        HARNESS_JS,
        re.S,
    )
    assert m, "post-rewrite context not found"
    after = m.group(1)
    # The toggle click bind must remain inside a guard.
    assert "if (sidebarToggle && layout) {" in after
    assert "sidebarToggle.addEventListener" in after


def test_session_list_rendered_after_reconcile():
    """reconcileActiveSession must call loadSessions (which renders the
    session list) before returning."""
    assert "await loadSessions();" in HARNESS_JS
    assert "reconcileActiveSession" in HARNESS_JS
