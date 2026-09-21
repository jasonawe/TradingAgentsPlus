"""Task 24 — architecture constraints (cutover guard).

Lightweight architectural assertions:
- Harness.from_data_dir exposes runtime components (Task 19 wiring)
- TurnCoordinator is the canonical Tier 1/2/3 entry point
- Web adapter module exposes chat_handler / resume_handler / trace_handler
"""
import pytest


def test_harness_exposes_runtime_components_after_init():
    from tradingagents.agent_harness.harness import Harness
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        from pathlib import Path
        h = Harness.from_data_dir(Path(d))
        assert h.runtime_store is not None
        assert h.scheduler is not None
        assert h.dispatcher is not None
        assert h.trace_projector is not None
        assert h.policy is not None


def test_turn_coordinator_module_exists_and_exports_turn_coordinator():
    from tradingagents.agent_harness.core import turn_coordinator
    assert hasattr(turn_coordinator, "TurnCoordinator")


def test_web_runtime_api_module_exposes_handlers():
    from web import harness_runtime_api
    assert hasattr(harness_runtime_api, "chat_handler")
    assert hasattr(harness_runtime_api, "resume_handler")
    assert hasattr(harness_runtime_api, "trace_handler")


def test_runtime_module_exposes_facade():
    from tradingagents.agent_harness.runtime import runtime as rt
    assert hasattr(rt, "AgentRuntime")
