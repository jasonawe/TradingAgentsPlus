"""Step 33 — external Workflow YAML spec loader."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ------------------------------------------------------------------
# Successful load
# ------------------------------------------------------------------
def test_loads_minimal_workflow():
    from tradingagents.agent_harness.core.workflow_yaml import (
        load_workflow_yaml,
    )
    yaml_text = """
name: hello
nodes:
  - id: a
    handler: tests.test_step33_workflow_yaml:_echo_handler
  - id: b
    handler: tests.test_step33_workflow_yaml:_echo_handler
edges:
  - from: a
    to: b
  - from: b
    to: "<end>"
entry: a
"""
    wf = load_workflow_yaml(yaml_text)
    assert not isinstance(wf, list), f"errors: {wf}"
    assert wf.name == "hello"
    assert [n.id for n in wf.nodes()] == ["a", "b"]


import pytest

@pytest.mark.asyncio
async def test_loads_workflow_with_conditions():
    from tradingagents.agent_harness.core.workflow_yaml import (
        load_workflow_yaml,
    )

    def _is_intent_quote(state):
        return state.get("intent") == "quote"

    yaml_text = """
name: branching
nodes:
  - id: classify
    handler: tests.test_step33_workflow_yaml:_passthrough
  - id: data_path
    handler: tests.test_step33_workflow_yaml:_passthrough
  - id: crud_path
    handler: tests.test_step33_workflow_yaml:_passthrough
edges:
  - from: classify
    to: data_path
    condition: is_quote
  - from: classify
    to: crud_path
entry: classify
"""
    wf = load_workflow_yaml(
        yaml_text,
        conditions={"is_quote": _is_intent_quote},
    )
    assert not isinstance(wf, list), f"errors: {wf}"

    # Run with intent=quote → should hit data_path
    events = await _collect(wf, {"intent": "quote"})
    assert wf.executed() == ["classify", "data_path"]


@pytest.mark.asyncio
async def test_loads_workflow_with_inline_callable_handlers():
    """handlers dict can pre-register Python callables."""
    from tradingagents.agent_harness.core.workflow_yaml import (
        load_workflow_yaml,
    )

    def _stub(state):
        state["ran"] = True
        from tradingagents.agent_harness.core.workflow import NodeResult
        return NodeResult(state=state, emit=[("done", {})])

    yaml_text = """
name: stub
nodes:
  - id: a
    handler: stub_fn
edges:
  - from: a
    to: "<end>"
entry: a
"""
    wf = load_workflow_yaml(yaml_text, handlers={"stub_fn": _stub})
    assert not isinstance(wf, list)
    events = await _collect(wf, {"x": 1})
    assert any(e[0] == "done" for e in events)


# ------------------------------------------------------------------
# Validation errors
# ------------------------------------------------------------------
def test_invalid_yaml_returns_errors():
    from tradingagents.agent_harness.core.workflow_yaml import (
        load_workflow_yaml,
    )
    result = load_workflow_yaml("not: valid: yaml: at: all: :")
    assert isinstance(result, list)
    assert any("parse" in e.lower() or "yaml" in e.lower() for e in result)


def test_spec_without_nodes_returns_error():
    from tradingagents.agent_harness.core.workflow_yaml import (
        load_workflow_yaml,
    )
    result = load_workflow_yaml("name: empty\nentry: a\n")
    assert isinstance(result, list)
    assert any("no nodes" in e for e in result)


def test_spec_without_entry_returns_error():
    from tradingagents.agent_harness.core.workflow_yaml import (
        load_workflow_yaml,
    )
    yaml_text = """
name: x
nodes:
  - id: a
    handler: foo.bar
"""
    result = load_workflow_yaml(yaml_text)
    assert isinstance(result, list)
    assert any("entry" in e for e in result)


def test_duplicate_node_id_returns_error():
    from tradingagents.agent_harness.core.workflow_yaml import (
        load_workflow_yaml,
    )
    yaml_text = """
name: dup
nodes:
  - id: a
    handler: foo
  - id: a
    handler: bar
entry: a
"""
    result = load_workflow_yaml(yaml_text)
    assert isinstance(result, list)
    assert any("duplicate" in e for e in result)


def test_unknown_handler_returns_error():
    from tradingagents.agent_harness.core.workflow_yaml import (
        load_workflow_yaml,
    )
    yaml_text = """
name: bad
nodes:
  - id: a
    handler: this.module.does.not.exist
entry: a
"""
    result = load_workflow_yaml(yaml_text)
    assert isinstance(result, list)


def test_edge_with_unknown_source_returns_error():
    from tradingagents.agent_harness.core.workflow_yaml import (
        load_workflow_yaml,
    )
    yaml_text = """
name: bad-edge
nodes:
  - id: a
    handler: foo
edges:
  - from: ghost
    to: a
entry: a
"""
    result = load_workflow_yaml(yaml_text)
    assert isinstance(result, list)
    assert any("ghost" in e for e in result)


def test_unregistered_condition_returns_error():
    from tradingagents.agent_harness.core.workflow_yaml import (
        load_workflow_yaml,
    )
    yaml_text = """
name: bad-cond
nodes:
  - id: a
    handler: foo
  - id: b
    handler: bar
edges:
  - from: a
    to: b
    condition: not_registered
entry: a
"""
    result = load_workflow_yaml(yaml_text)
    assert isinstance(result, list)
    assert any("not_registered" in e for e in result)


# ------------------------------------------------------------------
# End-to-end roundtrip
# ------------------------------------------------------------------
def test_roundtrip_yaml_text():
    """YAML -> Workflow -> introspection matches the source spec."""
    from tradingagents.agent_harness.core.workflow_yaml import (
        build_workflow_from_spec, load_workflow_yaml, spec_to_yaml_text,
    )
    spec = {
        "name": "roundtrip",
        "nodes": [
            {"id": "a", "handler": "_echo_handler"},
            {"id": "b", "handler": "_echo_handler"},
        ],
        "edges": [
            {"from": "a", "to": "b"},
            {"from": "b", "to": "<end>"},
        ],
        "entry": "a",
    }
    text = spec_to_yaml_text(spec)
    wf = load_workflow_yaml(text, handlers={"_echo_handler": _echo_handler})
    assert not isinstance(wf, list)
    # Compare structurally (handlers differ but graph is the same).
    assert [n.id for n in wf.nodes()] == ["a", "b"]
    assert [(e.from_node, e.to_node) for e in wf.edges()] == [
        ("a", "b"), ("b", "<end>"),
    ]


# ------------------------------------------------------------------
# Sync handlers get auto-wrapped
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sync_handler_auto_wrapped_to_async():
    """Sync handlers passed as handler refs should work in async workflow."""
    from tradingagents.agent_harness.core.workflow import (
        NodeResult, Workflow, Edge, Node,
    )
    from tradingagents.agent_harness.core.workflow_yaml import (
        build_workflow_from_spec,
    )

    def sync_handler(state):
        state["sync_ran"] = True
        return NodeResult(state=state, emit=[("sync_done", {})])

    spec = {
        "name": "sync-test",
        "nodes": [{"id": "a", "handler": sync_handler}],
        "edges": [{"from": "a", "to": "<end>"}],
        "entry": "a",
    }
    wf = build_workflow_from_spec(spec)
    async def _run():
        out = []
        async for ev, payload in wf.run({}):
            out.append((ev, payload))
        return out
    events = await _run()
    assert any(e[0] == "sync_done" for e in events)


# ------------------------------------------------------------------
# Helpers used by dotted handler refs above
# ------------------------------------------------------------------
async def _echo_handler(state):
    from tradingagents.agent_harness.core.workflow import NodeResult
    state["echoed"] = True
    return NodeResult(state=state, emit=[("echoed", {})])


async def _passthrough(state):
    from tradingagents.agent_harness.core.workflow import NodeResult
    return NodeResult(state=state, emit=[])


async def _collect(wf, state):
    out = []
    async for ev, payload in wf.run(state):
        out.append((ev, payload))
    return out
