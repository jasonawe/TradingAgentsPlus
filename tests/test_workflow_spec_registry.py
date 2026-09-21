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
    # V2: no default YAML-backed specs (Task 21). Shims return [].
    specs = registry.list()
    assert specs == []  # V2 dataclass registry is empty by default


def test_registry_loads_post_execute():
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
        WorkflowSpec,
    )
    # V2: register a dataclass spec, then retrieve it.
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    registry.register(WorkflowSpec(name="post-execute", steps=(), required_agents=("a",)))
    spec = registry.get("post-execute")
    assert isinstance(spec, WorkflowSpec)
    assert spec.name == "post-execute"
    assert "a" in spec.required_agents


def test_registry_caches_loads():
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
        WorkflowSpec,
    )
    # V2: get() returns the same dataclass instance on repeated calls.
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    registry.register(WorkflowSpec(name="post-execute", steps=(), required_agents=()))
    a = registry.get("post-execute")
    b = registry.get("post-execute")
    assert a is b


def test_registry_unknown_returns_errors():
    import pytest
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
    )
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    # V2: get() raises for missing specs (was list[str] in V1).
    with pytest.raises(KeyError):
        registry.get("does-not-exist")
    # The V1 shim get_yaml_text() still returns a list of errors.
    errs = registry.get_yaml_text("does-not-exist")
    assert isinstance(errs, list)
    assert any("not" in e.lower() or "no longer" in e for e in errs)


def test_registry_reload_clears_cache():
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
    )
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    # V2 has no YAML reload — the shim returns a list of errors.
    result = registry.reload("post-execute")
    assert isinstance(result, list)
    assert result  # non-empty error list


def test_registry_yaml_text_roundtrip():
    from tradingagents.agent_harness.core.workflow_spec_registry import (
        WorkflowSpecRegistry,
        WorkflowSpec,
    )
    registry = WorkflowSpecRegistry(orchestrator=_FakeOrchestrator())
    # V2 has no YAML — for an unknown name the shim returns errors.
    errs = registry.get_yaml_text("post-execute")
    assert isinstance(errs, list)
    assert any("no longer supported" in e for e in errs)
    # V2 can still surface a placeholder for registered dataclass specs.
    registry.register(WorkflowSpec(name="placeholder", steps=(), required_agents=()))
    text = registry.get_yaml_text("placeholder")
    assert isinstance(text, str)
    assert "placeholder" in text
