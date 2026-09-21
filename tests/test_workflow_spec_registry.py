"""Step 33 — WorkflowSpecRegistry tests."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class _FakeOrchestrator:
    """Minimal orchestrator stub exposing the bound methods the
    registry wraps. We don't actually run the workflows here —
    only check that the registry loads them."""

    async def _observe(self, state):
        return {"ok": True}

    async def _verify(self, state):
        class _Verdict:
            ok = True
            level = "L2"
            details = {}
        return _Verdict()

    async def _synthesize(self, state):
        return {"summary": "stub"}

    async def _call_tool(self, name, args):
        return {"tool": name, "args": args}


def test_registry_lists_default_specs():
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
    )
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    specs = registry.list()
    ids = {s["id"] for s in specs}
    assert {"post-execute", "classify-plan-execute", "parallel-fetch"} <= ids


def test_registry_loads_post_execute():
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
    )
    from tradingagents.agent_harness.core.workflow import Workflow
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    wf_or_errs = registry.get("post-execute")
    assert not isinstance(wf_or_errs, list), f"errors: {wf_or_errs}"
    assert isinstance(wf_or_errs, Workflow)
    # nodes() returns Node objects; iterate to get ids.
    node_ids = {n.id if hasattr(n, "id") else str(n) for n in wf_or_errs.nodes()}
    assert node_ids == {"observe", "verify", "synthesize"}


def test_registry_caches_loads():
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
    )
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    a = registry.get("post-execute")
    b = registry.get("post-execute")
    assert a is b  # cached


def test_registry_unknown_returns_errors():
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
    )
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    result = registry.get("does-not-exist")
    assert isinstance(result, list)
    assert any("not found" in e for e in result)


def test_registry_reload_clears_cache():
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
    )
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    a = registry.get("post-execute")
    b = registry.reload("post-execute")
    assert a is not b  # reload rebuilds


def test_registry_yaml_text_roundtrip():
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
    )
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    text = registry.get_yaml_text("post-execute")
    assert isinstance(text, str)
    assert "post-execute" in text
    assert "observe" in text
