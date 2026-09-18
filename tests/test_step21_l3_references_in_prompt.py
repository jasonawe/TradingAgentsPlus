"""Spec Step 21 — L3 references feed into the final synthesize prompt."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def l3():
    from tradingagents.agent_harness.memory.l3_references import AgentReferencesMemory
    with tempfile.TemporaryDirectory() as td:
        yield AgentReferencesMemory(db_path=Path(td) / "refs.sqlite")


def test_l3_list_filter_by_kind(l3):
    l3.set("quotes:600036.SS", {"price": 40.93}, ttl_seconds=300)
    l3.set("quotes:NVDA", {"price": 123.45}, ttl_seconds=300)
    l3.set("notes:abc", {"body": "..."}, ttl_seconds=300)
    out = l3.list(prefix="quotes:")
    assert len(out) == 2
    keys = {e.key for e in out}
    assert "quotes:600036.SS" in keys
    assert "quotes:NVDA" in keys


def test_l3_recall_symbol(l3):
    l3.set("quotes:600036.SS", {"price": 40.93}, ttl_seconds=300)
    e = l3.get("quotes:600036.SS")
    assert e is not None
    assert e.value == {"price": 40.93}


def _make_orch_with_memory(memory_obj):
    """Build an Orchestrator stub with a real memory attribute."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.memory = memory_obj
    return orch


def test_build_synth_prompt_includes_l3_block(l3):
    """When L3 has prior discussion records, the synthesize prompt
    surfaces them so the LLM can scope its answer."""
    l3.set(
        "discussions:harness-old-1",
        {"symbols": ["NVDA"], "summary": "深度分析 NVDA 估值"},
        ttl_seconds=86400,
    )

    from tradingagents.agent_harness.memory.manager import MemoryManager
    mm = MemoryManager.__new__(MemoryManager)
    mm.l3 = l3

    from tradingagents.agent_harness.core.orchestrator import Orchestrator, OrchestratorState
    state = OrchestratorState(
        session_id="harness-current",
        user_message="600036.SS 多少钱",
        symbols=["600036.SS"],
    )

    orch = _make_orch_with_memory(mm)
    prompt = Orchestrator._build_synthesize_prompt(orch, state)
    assert "L3" in prompt
    assert "NVDA" in prompt


def test_build_synth_prompt_no_l3_data_still_renders(l3):
    """Empty L3 still produces a valid synthesis prompt (no crash)."""
    from tradingagents.agent_harness.memory.manager import MemoryManager
    mm = MemoryManager.__new__(MemoryManager)
    mm.l3 = l3

    from tradingagents.agent_harness.core.orchestrator import Orchestrator, OrchestratorState
    state = OrchestratorState(
        session_id="harness-current",
        user_message="600036.SS 多少钱",
        symbols=["600036.SS"],
    )
    orch = _make_orch_with_memory(mm)
    prompt = Orchestrator._build_synthesize_prompt(orch, state)
    assert "User message" in prompt
    assert "L3" in prompt


def test_build_synth_prompt_no_memory_attribute_is_safe():
    """When memory is None (default factory path), prompt still renders."""
    from tradingagents.agent_harness.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.memory = None

    from tradingagents.agent_harness.core.orchestrator import Orchestrator, OrchestratorState
    state = OrchestratorState(
        session_id="harness-current",
        user_message="600036.SS 多少钱",
        symbols=["600036.SS"],
    )
    prompt = Orchestrator._build_synthesize_prompt(orch, state)
    assert "User message" in prompt
