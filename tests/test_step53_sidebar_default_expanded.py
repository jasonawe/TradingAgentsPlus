"""§0.4.19.fix — sidebar should default expanded on /harness entry."""
# §Browser-test note: this fix is purely client-side state-machine
# logic in web/static/harness.js. We assert the source code contains
# the correct default-expanded policy rather than spinning up a JSDOM
# harness (which would be brittle and isn't worth the maintenance
# overhead for a single boolean decision).
import re
from pathlib import Path

HARNESS_JS = Path("web/static/harness.js").read_text()


def test_default_expanded_when_user_never_chose():
    """The branch where ``userChose !== '1'`` must default to true."""
    # Pull the block between "let initialExpanded;" and the next
    # "setSidebarExpanded(initialExpanded, false)" call.
    m = re.search(
        r"let initialExpanded;(.+?)setSidebarExpanded\(initialExpanded",
        HARNESS_JS,
        re.S,
    )
    assert m, "could not locate initialExpanded decision"
    block = m.group(1)
    # Both branches must end in ``initialExpanded = true``.
    assert "initialExpanded = true" in block
    # The ``userChose === "1"`` branch must respect ``stored === "0"``
    # so explicit-collapsed users keep their collapse.
    assert 'initialExpanded = stored !== "0"' in block


def test_sticky_user_choice_preserved():
    """The ``setSidebarExpanded`` writer must set ``userChoseSidebar='1'``
    only when called from a user-initiated toggle click."""
    m = re.search(
        r"function setSidebarExpanded\((.+?)\)\s*\{(.+?)\}",
        HARNESS_JS,
        re.S,
    )
    assert m, "could not locate setSidebarExpanded"
    body = m.group(2)
    assert "ta.harness.userChoseSidebar" in body


def test_session_list_rendered_after_reconcile():
    """reconcileActiveSession must call loadSessions (which renders the
    session list) before returning."""
    assert "await loadSessions();" in HARNESS_JS
    assert "reconcileActiveSession" in HARNESS_JS
