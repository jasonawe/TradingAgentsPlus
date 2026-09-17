"""E2E — read-only multi-intent routes to Tier 1 short-circuit (no LLM).

Verifies the wire-level behaviour:
- '看一下 600036.SS 的笔记和告警' → 2 tool_calls + 2 tool_results
  + 1 agent_final with result.multi=[2 sections], NO plan_started,
  NO synthesize LLM call.
- Single-intent Tier 1 (read) still routes correctly (no multi).
- Write multi-intent stays at Tier 2 (plan + execute + synthesize).

Requires the harness web service to be running at 127.0.0.1:8000.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

BASE = "http://127.0.0.1:8000"


def _post(path: str, body: dict):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=20)


def _parse_sse(text: str):
    cur = None
    data = []
    for line in text.split("\n"):
        if line.startswith("event: "):
            cur = line[7:].strip()
        elif line.startswith("data: "):
            data.append(line[6:])
        elif line.strip() == "" and cur and data:
            try:
                payload = json.loads("\n".join(data))
            except Exception:
                payload = {"raw": "\n".join(data)}
            yield cur, payload
            cur = None
            data = []


def _run_chat(message: str) -> list[tuple[str, dict]]:
    sid = "e2e-multi-" + str(time.time_ns())
    with _post("/api/harness/chat", {
        "session_id": sid, "message": message,
    }) as resp:
        return list(_parse_sse(resp.read().decode()))


def _service_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/api/harness/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _service_up(),
    reason="harness web service not running at 127.0.0.1:8000",
)


def test_multi_intent_read_no_plan_emitted():
    """Read-only multi-intent must NOT emit plan_started / plan_ready."""
    events = _run_chat("看一下 600036.SS 的笔记和告警")
    names = [n for n, _ in events]

    # Tier 1 short-circuit emits: tool_call, tool_result, ..., agent_final
    # Tier 2 would emit plan_started + plan_ready + step/started/ended etc.
    assert "plan_started" not in names, (
        f"plan_started emitted (Tier 2 should not run): {names}"
    )
    assert "plan_ready" not in names, (
        f"plan_ready emitted (Tier 2 should not run): {names}"
    )

    # 2 tool_calls (one per intent)
    tool_calls = [p for n, p in events if n == "tool_call"]
    assert len(tool_calls) == 2, (
        f"expected 2 tool_calls, got {len(tool_calls)}: {tool_calls}"
    )

    # 2 tool_results
    tool_results = [p for n, p in events if n == "tool_result"]
    assert len(tool_results) == 2

    # agent_final has multi shape
    final = next(p for n, p in events if n == "agent_final")
    assert final.get("tier") == 1, f"tier should be 1, got {final.get('tier')}"
    result = final.get("result", {})
    assert "multi" in result, f"missing multi: {result}"
    assert isinstance(result["multi"], list)
    assert result["count"] == 2
    intents = {s["intent"] for s in result["multi"]}
    assert intents == {"note", "alert"}


def test_multi_intent_read_other_combinations():
    """Various read-only multi-intent pairs route to Tier 1."""
    cases = [
        ("我的关注 + 600036 的笔记", {"watchlist", "note"}),
    ]
    for msg, expected_intents in cases:
        events = _run_chat(msg)
        names = [n for n, _ in events]
        assert "plan_started" not in names, (
            f"{msg!r} emitted plan_started (Tier 2): {names}"
        )
        final = next(p for n, p in events if n == "agent_final")
        assert final.get("tier") == 1
        intents = {s["intent"] for s in final["result"]["multi"]}
        assert intents == expected_intents, (
            f"{msg!r}: expected {expected_intents}, got {intents}"
        )


def test_single_intent_read_still_tier1():
    """Single-intent read still routes to Tier 1 (regression check)."""
    events = _run_chat("600036 现在多少钱")
    names = [n for n, _ in events]
    assert "plan_started" not in names
    final = next(p for n, p in events if n == "agent_final")
    assert final.get("tier") == 1
    # NOT multi shape
    assert "multi" not in final.get("result", {})


def test_write_multi_still_tier2():
    """Write multi-intent must NOT short-circuit — needs HITL."""
    events = _run_chat("把 600036 加关注 + 加笔记")
    names = [n for n, _ in events]
    # Tier 2 emits plan_started, plan_ready, etc.
    assert "plan_started" in names, (
        f"write multi should still hit Tier 2: {names}"
    )
    # NO multi shape in agent_final (it's LLM-synthesized)
    final = next((p for n, p in events if n == "agent_final"), None)
    if final:
        # The write may pause at confirm_request before agent_final,
        # in which case there's no agent_final in this turn. That's OK.
        assert "multi" not in final.get("result", {})


def test_single_write_still_tier2():
    """Single write still routes to Tier 2 (regression check)."""
    events = _run_chat("给 600036 加个笔记: e2e regression")
    names = [n for n, _ in events]
    assert "plan_started" in names
    assert "confirm_request" in names, (
        f"single write should pause at confirm_request: {names}"
    )
