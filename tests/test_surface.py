"""P0-4 Surface routing — classify + tag + dispatch."""
from __future__ import annotations

import logging

import pytest

from tradingagents.agent_harness.core.surface import (
    DEFAULT_BY_EVENT,
    Surface,
    SurfaceRouter,
    classify,
)


# --------------------------------------------------------------------------
# classify — static lookup
# --------------------------------------------------------------------------
class TestClassify:
    def test_known_events(self):
        assert classify("plan_started") is Surface.UI
        assert classify("agent_final") is Surface.UI
        assert classify("usage_summary") is Surface.DEBUG
        assert classify("audit") is Surface.AUDIT

    def test_unknown_event_defaults_to_ui(self):
        # Conservative default: visible. Better to over-show than miss.
        assert classify("totally_new_event") is Surface.UI

    def test_default_map_includes_core_events(self):
        # Sanity: every event the orchestrator emits today must be classified.
        for ev in (
            "plan_started", "plan_ready", "plan_ready_ptc",
            "tool_result", "observed", "verified",
            "agent_final", "answer_verified", "usage_summary",
            "busy", "error",
        ):
            assert ev in DEFAULT_BY_EVENT, f"{ev} missing from DEFAULT_BY_EVENT"


# --------------------------------------------------------------------------
# tag — adds surface field to payload
# --------------------------------------------------------------------------
class TestTag:
    def test_adds_surface_for_known_event(self):
        r = SurfaceRouter()
        out = r.tag("usage_summary", {"total_tokens": 100})
        assert out["surface"] == "debug"
        assert out["total_tokens"] == 100

    def test_does_not_mutate_input(self):
        r = SurfaceRouter()
        original = {"x": 1}
        out = r.tag("plan_started", original)
        assert original == {"x": 1}  # input untouched
        assert out == {"x": 1, "surface": "ui"}

    def test_preserves_existing_surface_override(self):
        """Caller-set surface wins over the default classification."""
        r = SurfaceRouter()
        out = r.tag("plan_started", {"surface": "internal"})
        assert out["surface"] == "internal"

    def test_unknown_event_tags_as_ui(self):
        r = SurfaceRouter()
        out = r.tag("new_event", {"a": 1})
        assert out["surface"] == "ui"


# --------------------------------------------------------------------------
# route — side-effect dispatch
# --------------------------------------------------------------------------
class TestRoute:
    def test_routes_to_audit_sink(self):
        captured = []

        def _sink(event, payload):
            captured.append((event, payload))

        r = SurfaceRouter(audit_sink=_sink)
        r.route("audit", {"detail": "tier2_complete"})
        assert captured == [("audit", {"detail": "tier2_complete"})]

    def test_routes_debug_to_logger(self):
        logs = []

        class _Capture(logging.Logger):
            def debug(self, msg, *args, **kwargs):
                logs.append(msg % args if args else msg)

        r = SurfaceRouter(debug_logger=_Capture("capture"))
        r.route("usage_summary", {"total_tokens": 100})
        assert logs and "usage_summary" in logs[0]
        assert "100" in logs[0]

    def test_no_audit_sink_is_noop(self):
        r = SurfaceRouter()  # no sink
        # Must not raise.
        r.route("audit", {"x": 1})

    def test_audit_sink_exception_swallowed(self):
        def _boom(event, payload):
            raise RuntimeError("audit storage down")

        r = SurfaceRouter(audit_sink=_boom)
        # Must not raise; debug-logged.
        r.route("audit", {"x": 1})

    def test_ui_event_does_not_dispatch(self):
        """UI events are not dispatched to any sink (they flow to SSE)."""
        captured = []
        r = SurfaceRouter(audit_sink=lambda e, p: captured.append(e))
        r.route("plan_started", {})
        assert captured == []


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------
class TestIntrospection:
    def test_audit_sink_round_trip(self):
        sentinel = lambda e, p: None
        r = SurfaceRouter(audit_sink=sentinel)
        assert r.audit_sink is sentinel

    def test_debug_logger_defaults_to_module_logger(self):
        r = SurfaceRouter()
        # The default logger should be the module logger.
        assert r.debug_logger.name == "tradingagents.agent_harness.core.surface"
