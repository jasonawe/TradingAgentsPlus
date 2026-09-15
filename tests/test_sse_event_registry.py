"""W3-D4 E8: SSE Event Definition registry.

Static checks on ``web/static/app.js`` — Python can't actually load the
JS, but we can verify the structural invariants: the registry exists,
every expected event is registered, and the connect loop reads from
``listSseEventNames()`` instead of a hard-coded array.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

APP_JS = _ROOT / "web" / "static" / "app.js"

EXPECTED_EVENTS = [
    "run_started", "phase_changed", "agent_status", "progress",
    "message", "activity", "run_completed", "run_failed",
    "run_cancelled", "run_interrupted", "run_timed_out",
]


def _read_app_js() -> str:
    assert APP_JS.exists(), f"app.js not found at {APP_JS}"
    return APP_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Registry scaffolding
# ---------------------------------------------------------------------------


def test_sse_event_defs_registry_declared() -> None:
    src = _read_app_js()
    assert "const SSE_EVENT_DEFS" in src, "SSE_EVENT_DEFS not declared"


def test_register_sse_event_helper_exists() -> None:
    src = _read_app_js()
    assert "function registerSseEvent(name, handler)" in src


def test_list_sse_event_names_helper_exists() -> None:
    src = _read_app_js()
    assert "function listSseEventNames()" in src


# ---------------------------------------------------------------------------
# All 11 expected events are registered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_name", EXPECTED_EVENTS)
def test_event_is_registered(event_name: str) -> None:
    src = _read_app_js()
    # Match the registration call (allow either single or double quotes)
    pattern = rf'registerSseEvent\(\s*["\']{re.escape(event_name)}["\']'
    assert re.search(pattern, src), f"{event_name!r} not registered via registerSseEvent()"


def test_no_duplicate_event_registrations() -> None:
    """An event should be registered exactly once (a duplicate would
    throw at startup; we want a static check to catch the case before
    the page loads).
    """
    src = _read_app_js()
    for name in EXPECTED_EVENTS:
        pattern = rf'registerSseEvent\(\s*["\']{re.escape(name)}["\']'
        matches = re.findall(pattern, src)
        assert len(matches) == 1, (
            f"{name!r} registered {len(matches)} times (expected 1)"
        )


# ---------------------------------------------------------------------------
# connectEvents uses the registry, not a hardcoded list
# ---------------------------------------------------------------------------


def test_connect_events_uses_list_sse_event_names() -> None:
    src = _read_app_js()
    # Find connectEvents body and check it doesn't contain the old
    # hardcoded event-name list.
    # The old literal had: ["run_snapshot", "run_started", ...]
    hardcoded = re.compile(
        r'\["run_snapshot",\s*"run_started",\s*"phase_changed"'
    )
    assert not hardcoded.search(src), (
        "connectEvents still has the hardcoded event-name array"
    )
    assert "listSseEventNames().forEach" in src, (
        "connectEvents should iterate listSseEventNames()"
    )


# ---------------------------------------------------------------------------
# processEvent dispatches through the registry
# ---------------------------------------------------------------------------


def test_process_event_dispatches_via_registry() -> None:
    src = _read_app_js()
    # The new processEvent must call getSseEventHandler, not switch.
    assert "getSseEventHandler(envelope.event)" in src
    # And it should not have a giant switch on envelope.event
    # (legacy switch used `switch (envelope.event) { case "run_started": ... }`).
    assert "switch (envelope.event)" not in src, (
        "processEvent still has the old switch statement"
    )


# ---------------------------------------------------------------------------
# Adding a new event requires no framework change
# ---------------------------------------------------------------------------


def test_new_event_can_be_added_via_register_call() -> None:
    """The test is structural: if ``registerSseEvent`` exists and the
    11 built-ins are registered, then a 12th call is all that's needed.
    This is a regression guard against the switch statement creeping
    back.
    """
    src = _read_app_js()
    assert "registerSseEvent(" in src
    # count distinct registered names
    registered = re.findall(
        r'registerSseEvent\(\s*["\'](\w+)["\']', src,
    )
    assert len(registered) == len(EXPECTED_EVENTS), (
        f"expected {len(EXPECTED_EVENTS)} registered events, "
        f"found {len(registered)}: {registered}"
    )
    assert set(registered) == set(EXPECTED_EVENTS)
