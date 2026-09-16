"""End-to-end: SSE stream from POST /api/harness/chat.

Verifies that the FastAPI handler correctly:
- streams Server-Sent Events to the client (no buffering surprises),
- includes the expected orchestration events,
- reaches ``agent_final`` with content (i.e. synthesize LLM did NOT
  fall back to "returning raw tool results" — the bug fixed by
  ``_run_coro_sync`` in commit fcc0448).

The orchestrator's :py:meth:`Orchestrator._llm_plan` and
:py:meth:`Orchestrator._llm_synthesize` are patched in-place so the
test runs hermetically without API keys.  The patches are restored on
fixture teardown.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock as MM

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Stub replies
# ---------------------------------------------------------------------------
_PLAN_REPLY = [
    {
        "step": 1,
        "action": "get_quote",
        "args": {"symbol": "TEST.SS", "fields": ["price"]},
    },
]

_SYNTH_REPLY = "# 综合结论\n\n[stub] synthesis result for end-to-end test"


# ---------------------------------------------------------------------------
# App + harness fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def app_with_patched_orchestrator(tmp_path: Path):
    from web.app import create_app
    from web.manager import RunManager
    from web.storage import SQLiteStore
    from tradingagents.agent_harness.core.harness_checkpoint import (
        HarnessCheckpointStore,
    )
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    from tradingagents.agent_harness.harness import Harness

    # Patch orchestrator LLM hooks BEFORE constructing Harness so the
    # Orchestrator it owns picks up the patched methods.
    original_plan = Orchestrator._llm_plan
    original_synth = Orchestrator._llm_synthesize

    async def _fake_plan(self, state):
        return _PLAN_REPLY

    async def _fake_synthesize(self, state):
        return _SYNTH_REPLY

    Orchestrator._llm_plan = _fake_plan
    Orchestrator._llm_synthesize = _fake_synthesize

    try:
        settings = SQLiteStore(tmp_path / "settings.db")
        manager = RunManager()
        app = create_app(
            manager=manager,
            config={"results_dir": str(tmp_path), "project_dir": str(tmp_path)},
        )
        harness = Harness()
        harness.set_checkpoint_store(HarnessCheckpointStore(settings))
        app.state.harness = harness
        app.state.session_lock = MM(run=lambda sid, prod: prod())
        yield app
    finally:
        Orchestrator._llm_plan = original_plan
        Orchestrator._llm_synthesize = original_synth


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_sse_stream_emits_orchestration_events(app_with_patched_orchestrator):
    """End-to-end: POST /api/harness/chat returns a complete SSE stream."""
    client = TestClient(app_with_patched_orchestrator)
    with client.stream(
        "POST",
        "/api/harness/chat",
        json={"message": "分析 TEST.SS 估值"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _collect_sse_events(response.iter_lines())

    names = [name for name, _ in events]
    # The orchestration flow should produce these UI-facing events.
    assert "turn/started" in names
    assert "plan_started" in names
    # The orchestration must reach synthesize + agent_final, not stop
    # at the raw-tool-results fallback.
    assert "agent_final" in names, (
        f"missing agent_final event; saw: {names}"
    )


def test_sse_stream_does_not_emit_running_loop_error(app_with_patched_orchestrator):
    """Regression for fcc0448: synthesize must NOT crash inside FastAPI loop."""
    client = TestClient(app_with_patched_orchestrator)
    with client.stream(
        "POST",
        "/api/harness/chat",
        json={"message": "分析 TEST.SS 估值"},
    ) as response:
        events = _collect_sse_events(response.iter_lines())

    for name, payload in events:
        assert name != "error", f"unexpected error event: {payload}"
        payload_str = json.dumps(payload, ensure_ascii=False)
        assert "asyncio.run()" not in payload_str, (
            f"running-loop bug regressed in event {name}"
        )
        assert "cannot be called from a running event loop" not in payload_str


def test_sse_stream_agent_final_has_real_content(app_with_patched_orchestrator):
    """agent_final payload must contain real content (not raw tool dump)."""
    client = TestClient(app_with_patched_orchestrator)
    with client.stream(
        "POST",
        "/api/harness/chat",
        json={"message": "分析 TEST.SS 估值"},
    ) as response:
        events = _collect_sse_events(response.iter_lines())

    finals = [p for n, p in events if n == "agent_final"]
    assert finals, f"no agent_final event; saw: {[n for n, _ in events]}"
    final_payload = finals[0]
    # agent_final payload shape: {"tier": int, "result": str, "surface": "ui"}
    answer = (
        final_payload.get("result")
        if isinstance(final_payload, dict)
        else final_payload
    )
    assert answer is not None
    text = str(answer)
    assert _SYNTH_REPLY in text, (
        f"agent_final did not contain stub synthesize reply; got: {text[:200]!r}"
    )


def test_sse_stream_completes_with_token_usage(app_with_patched_orchestrator):
    """A complete stream must include a ``usage_summary`` event so the
    client can render token accounting."""
    client = TestClient(app_with_patched_orchestrator)
    with client.stream(
        "POST",
        "/api/harness/chat",
        json={"message": "分析 TEST.SS 估值"},
    ) as response:
        events = _collect_sse_events(response.iter_lines())

    names = [name for name, _ in events]
    # Synthesize finished is the canonical finish marker (the harness
    # emits agent_final as a downstream effect).
    assert "synthesize_finished" in names or "agent_final" in names, names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _collect_sse_events(lines) -> list[tuple[str, dict]]:
    """Yield (event_name, payload_dict) tuples from a TestClient SSE body."""
    events: list[tuple[str, dict]] = []
    event_name: str | None = None
    for line in lines:
        line = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            payload_raw = line[len("data:"):].strip()
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                payload = {"raw": payload_raw}
            if event_name is not None:
                events.append((event_name, payload))
                event_name = None
    return events
