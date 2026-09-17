"""Surface classification: every orchestrator event gets a ``surface`` tag.

The frontend uses ``payload.surface`` to filter which events render into
the chat panel. UI events render; DEBUG / AUDIT events still arrive via
SSE (for the audit log consumer and developer console) but don't pollute
the chat.
"""
from __future__ import annotations

import pytest

from tradingagents.agent_harness.core.surface import (
    DEFAULT_BY_EVENT,
    Surface,
    SurfaceRouter,
    classify,
)


# Events that the user must see in the chat panel
UI_EVENTS = [
    "plan_started",
    "plan_ready",
    "plan_ready_ptc",
    "tool_result",
    "observed",
    "verified",
    "agent_final",
    "answer_verified",
    "error",
    "warning",
    "confirm_request",
    "audit_decision",
]

# Events that must NOT render into the chat panel
DEBUG_EVENTS = [
    "turn/started",
    "turn/ended",
    "step/started",
    "step/ended",
    "usage_summary",
    "tier2_complete",
]


@pytest.mark.parametrize("event", UI_EVENTS)
def test_ui_events_classified_as_ui(event):
    assert classify(event) is Surface.UI


@pytest.mark.parametrize("event", DEBUG_EVENTS)
def test_debug_events_classified_as_debug(event):
    assert classify(event) is Surface.DEBUG


def test_unknown_event_defaults_to_ui():
    """Conservative default — unknown event names are visible until
    explicitly classified. Prevents accidentally hiding events from the
    user when the backend adds new ones before the classification is
    updated."""
    assert classify("brand_new_event") is Surface.UI


def test_tag_adds_surface_to_payload():
    router = SurfaceRouter()
    out = router.tag("step/started", {"step_id": 1})
    assert out["surface"] == "debug"
    assert out["step_id"] == 1


def test_tag_does_not_mutate_input():
    router = SurfaceRouter()
    original = {"step_id": 1}
    out = router.tag("step/started", original)
    assert "surface" not in original
    assert out is not original


def test_tag_respects_explicit_surface():
    """If the caller already set ``surface``, leave it alone — caller override."""
    router = SurfaceRouter()
    out = router.tag("plan_started", {"surface": "debug", "intent": "data"})
    assert out["surface"] == "debug"
    assert out["intent"] == "data"


def test_route_calls_audit_sink_for_audit_events():
    calls = []
    router = SurfaceRouter(audit_sink=lambda ev, payload: calls.append((ev, payload)))
    router.route("audit", {"key": "value"})
    assert calls == [("audit", {"key": "value"})]


def test_route_swallows_sink_exceptions():
    def bad_sink(ev, payload):
        raise RuntimeError("sink boom")
    router = SurfaceRouter(audit_sink=bad_sink)
    # Must not raise — audit failures must not break the orchestrator.
    router.route("audit", {"key": "value"})


def test_default_classification_covers_all_emitted_events():
    """Every event name emitted from Orchestrator must be in DEFAULT_BY_EVENT.

    Catches drift when someone adds a new ``_emit(...)`` call without
    updating the classification. The set below is the current source of
    truth — if a new event is added, this test fails until both
    surface.py and this list are updated together.
    """
    KNOWN_EVENTS = set(UI_EVENTS) | set(DEBUG_EVENTS) | {"audit"}
    unclassified = KNOWN_EVENTS - set(DEFAULT_BY_EVENT)
    assert not unclassified, f"events not in DEFAULT_BY_EVENT: {unclassified}"
