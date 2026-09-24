# [Phase 1 — Multi-Agent Runtime Skeleton] Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the type system, `GraphExecutor` skeleton, `PlanCompiler` (single-group case), and `consult_subagent` tool stub behind the `runtime.multi_agent` flag (default **off**) without changing any existing behaviour. All current tests must remain green.

**Architecture:** Mirror the spec §4 component diagram. New files live under `tradingagents/agent_harness/runtime/` and one new tool under `tradingagents/agent_harness/tools/builtin_consult.py`. The orchestrator gains a new code path (`if settings.runtime.multi_agent: ...`) but the existing PTC branch is untouched in this phase.

**Tech Stack:** Python 3.14.6, asyncio, Pydantic v2, dataclasses, `pytest-asyncio`. Reuses `ToolPipeline` from `tradingagents/agent_harness/tools/pipeline.py` and `AgentRegistry` from `tradingagents/agent_harness/agents/registry.py`.

**Spec:** `docs/superpowers/specs/2026-09-24-multi-agent-message-passing-runtime.md`

**Locked decisions (spec §8):**
- Q1=A declared inputs only (cross-agent access via `$ref` + edge)
- Q2=A re-execute upstream node on loop trigger (hop index for staleness)
- Q3=B consultation cap configurable 0.0–0.8 (default 0.5)
- Q4=B all agents (`data`/`alpha`/`news`/`trading_agents`) nestable
- Q5=B mid-flight state persisted via `AgentRuntimeStore` (lands in Phase 6)

## File Structure

| File | Responsibility |
|---|---|
| `tradingagents/agent_harness/runtime/__init__.py` | Public exports (`GraphSpec`, `GraphExecutor`, `PlanCompiler`, `NodeKind`, `Message`) |
| `tradingagents/agent_harness/runtime/graph.py` | `BaseNode` protocol, `NodeKind` enum, `GraphSpec`, `Edge` dataclasses + validators |
| `tradingagents/agent_harness/runtime/state.py` | `GraphState`, `TypedResult`, `Message`, `FieldRef` |
| `tradingagents/agent_harness/runtime/executor.py` | `GraphExecutor` skeleton (priority queue walker + activation check + budget guard) |
| `tradingagents/agent_harness/runtime/resolver.py` | `$ref` parser + evaluator (pure functions, falls back to literal) |
| `tradingagents/agent_harness/runtime/nodes.py` | `ToolNode`, `LLMNode`, `SubplanNode`, `ConsultNode` (Phase 1: `ToolNode.run` delegates to `ToolPipeline`; others raise `NotImplementedError`) |
| `tradingagents/agent_harness/runtime/compiler.py` | `PlanCompiler.compile(plan) -> GraphSpec` for flat parallel_group case |
| `tradingagents/agent_harness/runtime/agents.py` | Graph fragments for `data_agent`/`alpha_agent`/`news_agent`/`trading_agents` as data constants (no live execution in Phase 1) |
| `tradingagents/agent_harness/runtime/settings.py` | `RuntimeSettings` Pydantic model |
| `tradingagents/agent_harness/tools/builtin_consult.py` | `consult_subagent` tool stub |
| `tradingagents/agent_harness/core/orchestrator.py` | Flag-gated dispatch (`if settings.runtime.multi_agent`) |
| live settings YAML | Add `runtime.multi_agent: false` block |
| `tests/agent_harness/runtime/test_state.py` | State dataclass round-trip |
| `tests/agent_harness/runtime/test_graph.py` | GraphSpec validation + serialization |
| `tests/agent_harness/runtime/test_resolver.py` | `$ref` parser + evaluator |
| `tests/agent_harness/runtime/test_compiler.py` | PlanCompiler simple case |
| `tests/agent_harness/runtime/test_executor.py` | GraphExecutor walks 2-node linear graph; budget guard |
| `tests/agent_harness/runtime/test_settings.py` | RuntimeSettings defaults |
| `tests/agent_harness/tools/test_builtin_consult.py` | consult args + stub |

Phase 1 deliberately does NOT:
- Resolve `$ref` cross-agent reads at runtime (Phase 3)
- Execute LLM nodes (Phase 2)
- Spawn subplans (Phase 4)
- Persist mid-flight state (Phase 6)

These paths raise `NotImplementedError` in Phase 1.

---

## Chunk 1: Types & Settings

### Task 1: Runtime settings + config plumbing

**Files:**
- Create: `tradingagents/agent_harness/runtime/settings.py`
- Modify: live settings YAML (discover path in step 1.1)
- Test: `tests/agent_harness/runtime/test_settings.py`

- [ ] **Step 1.1: Locate live settings file**

```bash
rg -n "llm_router|llm_budget" web/ tradingagents/ | head -20
```

Note the path in the commit message.

- [ ] **Step 1.2: Write failing test**

```python
# tests/agent_harness/runtime/test_settings.py
from tradingagents.agent_harness.runtime.settings import RuntimeSettings

def test_runtime_settings_defaults():
    s = RuntimeSettings()
    assert s.multi_agent is False
    assert s.llm_budget_per_turn == 5
    assert s.max_hops == 8
    assert 0.0 <= s.consultation_rate_limit <= 0.8

def test_runtime_settings_override():
    s = RuntimeSettings(multi_agent=True, llm_budget_per_turn=2)
    assert s.multi_agent is True
    assert s.llm_budget_per_turn == 2
```

- [ ] **Step 1.3: Run, expect FAIL** (`ModuleNotFoundError`)

- [ ] **Step 1.4: Implement `RuntimeSettings`**

```python
# tradingagents/agent_harness/runtime/settings.py
from pydantic import BaseModel, Field

class RuntimeSettings(BaseModel):
    multi_agent: bool = False
    llm_budget_per_turn: int = Field(default=5, ge=1, le=50)
    max_hops: int = Field(default=8, ge=1, le=64)
    consultation_rate_limit: float = Field(default=0.5, ge=0.0, le=0.8)
```

- [ ] **Step 1.5: Run, expect PASS**

- [ ] **Step 1.6: Add config key to live settings file**

```yaml
runtime:
  multi_agent: false
  llm_budget_per_turn: 5
  max_hops: 8
  consultation_rate_limit: 0.5
```

- [ ] **Step 1.7: Commit**

```bash
git add tradingagents/agent_harness/runtime/settings.py \
        tests/agent_harness/runtime/test_settings.py \
        <discovered-settings-yaml>
git commit -m "feat(runtime): §0.4.35 phase 1 — add RuntimeSettings (multi_agent off by default)"
```

### Task 2: `GraphState` + `TypedResult` + `Message` + `FieldRef`

**Files:**
- Create: `tradingagents/agent_harness/runtime/state.py`
- Test: `tests/agent_harness/runtime/test_state.py`

- [ ] **Step 2.1: Write failing tests**

```python
# tests/agent_harness/runtime/test_state.py
import time
from tradingagents.agent_harness.runtime.state import (
    GraphState, TypedResult, Message, FieldRef,
)

def test_typed_result_roundtrip():
    class Quote:
        def __init__(self, price): self.price = price
    res = TypedResult(schema=Quote, data=Quote(41.06), meta={"source": "yfinance"})
    assert res.data.price == 41.06
    assert res.meta["source"] == "yfinance"

def test_message_defaults():
    m = Message(sender="a", receiver="b",
                payload=TypedResult(schema=dict, data={}, meta={}))
    assert m.kind == "data"
    assert m.hop == 0
    assert isinstance(m.ts, float)

def test_fieldref_str():
    r = FieldRef(agent="data", field="quote.symbol")
    assert str(r) == "\$data.quote.symbol"

def test_graph_state_consume_inbox():
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    msg = Message(sender="x", receiver="y",
                  payload=TypedResult(schema=dict, data={"a": 1}, meta={}))
    state.append_inbox(msg)
    inbox = state.consume_inbox("y")
    assert len(inbox) == 1
    assert state.consume_inbox("y") == []  # idempotent
```

- [ ] **Step 2.2: Run, expect FAIL**

- [ ] **Step 2.3: Implement `state.py`**

```python
# tradingagents/agent_harness/runtime/state.py
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Literal

@dataclass(frozen=True)
class FieldRef:
    agent: str
    field: str

    def __str__(self) -> str:
        return f"\${self.agent}.{self.field}"

@dataclass
class TypedResult:
    schema: type
    data: Any
    meta: dict

MessageKind = Literal["data", "question", "answer", "delta"]

@dataclass
class Message:
    sender: str
    receiver: str
    payload: TypedResult
    kind: MessageKind = "data"
    ts: float = field(default_factory=time.monotonic)
    hop: int = 0

@dataclass
class GraphState:
    run_id: str
    turn_id: str
    intent: str
    agent_outputs: dict[str, TypedResult] = field(default_factory=dict)
    inbox: dict[str, list[Message]] = field(default_factory=dict)
    message_log: list[Message] = field(default_factory=list)
    hops_remaining: int = 8
    llm_used: int = 0
    consultation_used: int = 0
    budget_limit: int = 5
    consultation_rate_limit: float = 0.5

    def append_inbox(self, msg: Message) -> None:
        self.inbox.setdefault(msg.receiver, []).append(msg)
        self.message_log.append(msg)

    def consume_inbox(self, receiver: str) -> list[Message]:
        return self.inbox.pop(receiver, [])

    def fork(self) -> "GraphState":
        return GraphState(
            run_id=self.run_id, turn_id=self.turn_id, intent=self.intent,
            agent_outputs=dict(self.agent_outputs), inbox={},
            message_log=list(self.message_log),
            hops_remaining=self.hops_remaining, llm_used=self.llm_used,
            consultation_used=self.consultation_used,
            budget_limit=self.budget_limit,
            consultation_rate_limit=self.consultation_rate_limit,
        )
```

- [ ] **Step 2.4: Run, expect PASS**

- [ ] **Step 2.5: Commit**

```bash
git add tradingagents/agent_harness/runtime/state.py \
        tests/agent_harness/runtime/test_state.py
git commit -m "feat(runtime): §0.4.35 phase 1 — GraphState, TypedResult, Message, FieldRef"
```

### Task 3: `GraphSpec` + `Edge` + `BaseNode` protocol + `NodeKind`

**Files:**
- Create: `tradingagents/agent_harness/runtime/graph.py`
- Test: `tests/agent_harness/runtime/test_graph.py`

- [ ] **Step 3.1: Write failing tests**

```python
# tests/agent_harness/runtime/test_graph.py
import pytest
from tradingagents.agent_harness.runtime.graph import (
    NodeKind, Edge, GraphSpec, BaseNode,
)
from tradingagents.agent_harness.runtime.state import FieldRef, TypedResult, Message

def test_node_kind_values():
    assert NodeKind.TOOL.value == "tool"
    assert NodeKind.LLM.value == "llm"
    assert NodeKind.SUBPLAN.value == "subplan"
    assert NodeKind.CONSULT.value == "consult"

def test_edge_required_kind():
    with pytest.raises(Exception):
        Edge(src="a", dst="b", kind="bogus")  # type: ignore[arg-type]

class _StubNode:
    id = "n1"
    agent_id = "data_agent"
    kind = NodeKind.TOOL
    inputs: list[FieldRef] = []
    outputs: list[FieldRef] = []
    async def run(self, state, inbox):
        return []

def test_graph_spec_node_lookup():
    g = GraphSpec(nodes={"n1": _StubNode()}, edges=[])
    assert g.node("n1").id == "n1"

def test_graph_spec_serialization_smoke():
    g = GraphSpec(
        nodes={"n1": _StubNode()},
        edges=[Edge(src="n1", dst="n2", kind="data")],
    )
    blob = g.model_dump_json()
    assert "n1" in blob
```

- [ ] **Step 3.2: Run, expect FAIL**

- [ ] **Step 3.3: Implement `graph.py`**

```python
# tradingagents/agent_harness/runtime/graph.py
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Optional, Protocol, runtime_checkable
from pydantic import BaseModel, Field

from .state import FieldRef, Message, TypedResult

class NodeKind(str, Enum):
    TOOL = "tool"
    LLM = "llm"
    SUBPLAN = "subplan"
    CONSULT = "consult"

@runtime_checkable
class BaseNode(Protocol):
    id: str
    agent_id: str
    kind: NodeKind
    inputs: list[FieldRef]
    outputs: list[FieldRef]
    async def run(self, state, inbox): ...

@dataclass
class Edge:
    src: str
    dst: str
    kind: Literal["data", "when", "loop"]
    field_ref: Optional[str] = None
    predicate: Optional[Any] = None   # callable(state, message) -> bool (when only)
    max_hops: int = 3

class GraphSpec(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    nodes: dict[str, BaseNode]
    edges: list[Edge] = Field(default_factory=list)
    entry: Optional[str] = None
    exit: Optional[str] = None

    def node(self, node_id: str) -> BaseNode:
        return self.nodes[node_id]

    def edges_from(self, src: str) -> list[Edge]:
        return [e for e in self.edges if e.src == src]
```

- [ ] **Step 3.4: Run, expect PASS**

- [ ] **Step 3.5: Commit**

```bash
git add tradingagents/agent_harness/runtime/graph.py \
        tests/agent_harness/runtime/test_graph.py
git commit -m "feat(runtime): §0.4.35 phase 1 — GraphSpec, Edge, BaseNode protocol"
```

### Task 4: Package `__init__.py`

**Files:**
- Create: `tradingagents/agent_harness/runtime/__init__.py`

- [ ] **Step 4.1: Write public exports**

```python
# tradingagents/agent_harness/runtime/__init__.py
from .graph import NodeKind, Edge, GraphSpec, BaseNode
from .state import GraphState, TypedResult, Message, FieldRef
from .settings import RuntimeSettings

__all__ = [
    "NodeKind", "Edge", "GraphSpec", "BaseNode",
    "GraphState", "TypedResult", "Message", "FieldRef",
    "RuntimeSettings",
]
```

- [ ] **Step 4.2: Verify import**

Run: `.venv/bin/python -c "from tradingagents.agent_harness.runtime import GraphSpec, RuntimeSettings; print(GraphSpec, RuntimeSettings)"`
Expected: prints both classes, no error.

- [ ] **Step 4.3: Commit**

```bash
git add tradingagents/agent_harness/runtime/__init__.py
git commit -m "feat(runtime): §0.4.35 phase 1 — package __init__ public API"
```

---

## Chunk 2: Resolver + Compiler

### Task 5: `$ref` parser + evaluator

**Files:**
- Create: `tradingagents/agent_harness/runtime/resolver.py`
- Test: `tests/agent_harness/runtime/test_resolver.py`

- [ ] **Step 5.1: Write failing tests**

```python
# tests/agent_harness/runtime/test_resolver.py
from tradingagents.agent_harness.runtime.resolver import (
    parse_ref, is_ref_expr, resolve_ref,
)
from tradingagents.agent_harness.runtime.state import GraphState, TypedResult

def _state():
    return GraphState(
        run_id="r", turn_id="t", intent="x",
        agent_outputs={
            "data": TypedResult(schema=dict, data={"quote": {"symbol": "600036.SS"}}, meta={}),
            "alpha": TypedResult(schema=dict, data={"price": 40.5}, meta={}),
        },
    )

def test_parse_ref_basic():
    assert parse_ref("\$data.quote.symbol") == ("data", "quote.symbol")

def test_parse_ref_no_dollar_raises():
    assert parse_ref("data.quote.symbol") is None

def test_is_ref_expr():
    assert is_ref_expr("\$data.x")
    assert not is_ref_expr("hello")
    assert is_ref_expr("\$data.x * 1.5")

def test_resolve_ref_simple():
    s = _state()
    assert resolve_ref("\$data.quote.symbol", s) == "600036.SS"

def test_resolve_ref_arithmetic():
    s = _state()
    assert resolve_ref("\$alpha.price * 2", s) == 81.0

def test_resolve_ref_falls_back_to_literal():
    s = _state()
    assert resolve_ref("\$nope.field", s, literal="fallback") == "fallback"
```

- [ ] **Step 5.2: Run, expect FAIL**

- [ ] **Step 5.3: Implement `resolver.py`**

```python
# tradingagents/agent_harness/runtime/resolver.py
"""Pure \$ref parser + evaluator for the multi-agent runtime."""
from __future__ import annotations
import re
from typing import Any, Optional

from .state import GraphState

_REF_RE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z0-9_.]+)")

def is_ref_expr(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return "\$" in value and bool(_REF_RE.search(value))

def parse_ref(value: str) -> Optional[tuple[str, str]]:
    if not isinstance(value, str) or not value.startswith("\$"):
        return None
    m = _REF_RE.match(value.strip())
    if not m:
        return None
    return m.group(1), m.group(2)

def _lookup(state: GraphState, agent: str, dotted: str) -> Any:
    if agent not in state.agent_outputs:
        return None
    node = state.agent_outputs[agent].data
    if isinstance(node, dict):
        cur: Any = node
        for part in dotted.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
            if cur is None:
                return None
        return cur
    for part in dotted.split("."):
        if not hasattr(node, part):
            return None
        node = getattr(node, part)
    return node

def _eval_expr(value: str, state: GraphState) -> Any:
    if "?" in value and ":" in value:
        cond, rest = value.split("?", 1)
        a, b = rest.split(":", 1)
        cond_val = _eval_expr(cond.strip(), state)
        chosen = a if _truthy(cond_val) else b
        return _eval_expr(chosen.strip(), state)
    for op, fn in ((" * ", lambda a, b: a * b),
                   (" + ", lambda a, b: a + b),
                   (" - ", lambda a, b: a - b),
                   (" / ", lambda a, b: a / b)):
        if op in value:
            left, right = value.split(op, 1)
            return fn(_eval_expr(left.strip(), state),
                      _eval_expr(right.strip(), state))
    ref = parse_ref(value.strip())
    if ref:
        agent, dotted = ref
        return _lookup(state, agent, dotted)
    try:
        return int(value.strip())
    except ValueError:
        try:
            return float(value.strip())
        except ValueError:
            stripped = value.strip()
            if stripped.startswith('"') and stripped.endswith('"'):
                return stripped[1:-1]
            return stripped

def _truthy(v: Any) -> bool:
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)

def resolve_ref(value: str, state: GraphState, *, literal: Any = None) -> Any:
    """Resolve a \$ref expression; on any failure return `literal`."""
    try:
        if not isinstance(value, str):
            return value
        if not is_ref_expr(value):
            return value
        return _eval_expr(value, state)
    except Exception:
        return literal
```

- [ ] **Step 5.4: Run, expect PASS**

- [ ] **Step 5.5: Commit**

```bash
git add tradingagents/agent_harness/runtime/resolver.py \
        tests/agent_harness/runtime/test_resolver.py
git commit -m "feat(runtime): §0.4.35 phase 1 — \$ref parser/evaluator with literal fallback"
```

### Task 6: `PlanCompiler` — single-group + sequential case

**Files:**
- Create: `tradingagents/agent_harness/runtime/compiler.py`
- Test: `tests/agent_harness/runtime/test_compiler.py`

- [ ] **Step 6.1: Locate `RouterPlan` and `_TOOL_TO_AGENT`**

```bash
rg -n "RouterPlan|class RouterPlan|parallel_group" tradingagents/agent_harness/core/llm_router.py | head
rg -n "_TOOL_TO_AGENT" tradingagents/agent_harness/core/orchestrator.py | head
```

Note paths for the import.

- [ ] **Step 6.2: Write failing tests**

```python
# tests/agent_harness/runtime/test_compiler.py
from tradingagents.agent_harness.runtime.compiler import PlanCompiler, CompileError
from tradingagents.agent_harness.runtime.graph import GraphSpec, NodeKind

class FakeCall:
    def __init__(self, tool, args, parallel_group, depends_on=None):
        self.tool = tool
        self.args = args
        self.parallel_group = parallel_group
        self.depends_on = depends_on or []

class FakePlan:
    def __init__(self, calls):
        self.calls = calls

def test_compiler_single_group_emits_one_tool_node_per_call_and_join():
    plan = FakePlan([
        FakeCall("get_quote", {"symbol": "600036.SS"}, parallel_group=0),
        FakeCall("get_fundamentals", {"symbol": "600036.SS"}, parallel_group=0),
    ])
    spec = PlanCompiler().compile(plan)
    tool_nodes = [n for n in spec.nodes.values() if n.kind == NodeKind.TOOL]
    assert len(tool_nodes) == 2
    assert {n.id for n in tool_nodes} == {"call_0", "call_1"}
    join_targets = {(e.src, e.dst) for e in spec.edges}
    assert ("call_0", "join_0") in join_targets
    assert ("call_1", "join_0") in join_targets

def test_compiler_two_groups_sequential_edges():
    plan = FakePlan([
        FakeCall("get_quote", {"symbol": "x"}, parallel_group=0),
        FakeCall("compute_alpha", {"symbol": "x"}, parallel_group=1, depends_on=[0]),
    ])
    spec = PlanCompiler().compile(plan)
    edges = {(e.src, e.dst) for e in spec.edges}
    assert ("call_0", "join_0") in edges
    assert ("call_1", "join_1") in edges
    assert ("join_0", "call_1") in edges

def test_compiler_rejects_unknown_tool():
    plan = FakePlan([FakeCall("definitely_not_a_tool", {}, parallel_group=0)])
    try:
        PlanCompiler().compile(plan)
    except CompileError:
        return
    raise AssertionError("expected CompileError")
```

- [ ] **Step 6.3: Run, expect FAIL**

- [ ] **Step 6.4: Implement `compiler.py`**

```python
# tradingagents/agent_harness/runtime/compiler.py
"""Phase 1: compiles a RouterPlan to a GraphSpec for the simple (no \$ref,
single-group or sequential multi-group) cases. Falls back to PTC semantics.
"""
from __future__ import annotations
from typing import Any

from .graph import GraphSpec, Edge, BaseNode
from .nodes import ToolNode
from ..core.orchestrator import _TOOL_TO_AGENT  # verified in Task 6.1

class CompileError(Exception):
    """Raised when a RouterPlan cannot be turned into a GraphSpec."""

class PlanCompiler:
    def compile(self, plan: Any) -> GraphSpec:
        calls = list(plan.calls)
        if not calls:
            raise CompileError("empty plan")

        nodes: dict[str, BaseNode] = {}
        edges: list[Edge] = []
        groups: dict[int, list[str]] = {}

        for idx, call in enumerate(calls):
            node_id = f"call_{idx}"
            agent = _TOOL_TO_AGENT.get(call.tool)
            if agent is None:
                raise CompileError(f"unknown tool: {call.tool}")
            nodes[node_id] = ToolNode(
                id=node_id,
                agent_id=agent,
                tool_name=call.tool,
                raw_args=dict(call.args),
            )
            groups.setdefault(call.parallel_group, []).append(node_id)

        for gid, nodelist in groups.items():
            join_id = f"join_{gid}"
            for nid in nodelist:
                edges.append(Edge(src=nid, dst=join_id, kind="data"))

        sorted_groups = sorted(groups.keys())
        for i in range(1, len(sorted_groups)):
            prev_g = sorted_groups[i - 1]
            cur_g = sorted_groups[i]
            for call in calls:
                if call.parallel_group != cur_g:
                    continue
                if prev_g in (call.depends_on or []):
                    edges.append(Edge(
                        src=f"join_{prev_g}",
                        dst=f"call_{calls.index(call)}",
                        kind="data",
                    ))
                    break

        entry = "call_0" if calls else None
        exit_node = f"join_{sorted_groups[-1]}" if sorted_groups else None
        return GraphSpec(nodes=nodes, edges=edges, entry=entry, exit=exit_node)
```

- [ ] **Step 6.5: Run, expect PASS** (will require `ToolNode` from Task 7)

- [ ] **Step 6.6: Commit**

```bash
git add tradingagents/agent_harness/runtime/compiler.py \
        tests/agent_harness/runtime/test_compiler.py
git commit -m "feat(runtime): §0.4.35 phase 1 — PlanCompiler (no \$ref, sequential groups)"
```

---

## Chunk 3: Node Implementations + Executor

### Task 7: `ToolNode` + stubs for LLM/Subplan/Consult

**Files:**
- Create: `tradingagents/agent_harness/runtime/nodes.py`

- [ ] **Step 7.1: Resolve tool dispatch signature**

```bash
rg -n "ToolPipeline|tool\\.invoke|tool\\.run" tradingagents/agent_harness/core/orchestrator.py | head
```

- [ ] **Step 7.2: Implement `nodes.py`**

```python
# tradingagents/agent_harness/runtime/nodes.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from .graph import BaseNode, NodeKind
from .state import FieldRef, GraphState, Message, TypedResult

class _NodeBase:
    id: str
    agent_id: str
    kind: NodeKind
    inputs: list[FieldRef]
    outputs: list[FieldRef]

    def __init__(self, id: str, agent_id: str, kind: NodeKind,
                 inputs: list[FieldRef] | None = None,
                 outputs: list[FieldRef] | None = None) -> None:
        self.id = id
        self.agent_id = agent_id
        self.kind = kind
        self.inputs = list(inputs or [])
        self.outputs = list(outputs or [])

class ToolNode(_NodeBase):
    def __init__(self, id: str, agent_id: str, tool_name: str, raw_args: dict[str, Any]):
        super().__init__(id, agent_id, NodeKind.TOOL)
        self.tool_name = tool_name
        self.raw_args = raw_args

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        # Phase 1: ToolNode delegates to the existing ToolPipeline so the
        # simple case works end-to-end. Cross-agent \$ref resolution lands
        # in Phase 3.
        from ..tools.pipeline import invoke_tool  # helper discovered in 7.1
        result = await invoke_tool(
            tool_name=self.tool_name,
            args=dict(self.raw_args or {}),
            session_id=state.run_id,
        )
        typed = TypedResult(schema=dict, data=result, meta={"tool": self.tool_name})
        return [Message(sender=self.id, receiver="*", payload=typed)]

class LLMNode(_NodeBase):
    def __init__(self, id: str, agent_id: str, system_prompt: str):
        super().__init__(id, agent_id, NodeKind.LLM)
        self.system_prompt = system_prompt

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        raise NotImplementedError("LLMNode lands in Phase 2")

class SubplanNode(_NodeBase):
    def __init__(self, id: str, agent_id: str, sub_graph):
        super().__init__(id, agent_id, NodeKind.SUBPLAN)
        self.sub_graph = sub_graph

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        raise NotImplementedError("SubplanNode lands in Phase 4")

class ConsultNode(_NodeBase):
    def __init__(self, id: str, agent_id: str, target_agent: str, question: str):
        super().__init__(id, agent_id, NodeKind.CONSULT)
        self.target_agent = target_agent
        self.question = question

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        raise NotImplementedError("ConsultNode lands in Phase 2")
```

- [ ] **Step 7.3: Commit**

```bash
git add tradingagents/agent_harness/runtime/nodes.py
git commit -m "feat(runtime): §0.4.35 phase 1 — node stubs (ToolNode live, others Phase 2+)"
```

### Task 8: `GraphExecutor` skeleton

**Files:**
- Create: `tradingagents/agent_harness/runtime/executor.py`
- Test: `tests/agent_harness/runtime/test_executor.py`

- [ ] **Step 8.1: Write failing tests**

```python
# tests/agent_harness/runtime/test_executor.py
import asyncio
from tradingagents.agent_harness.runtime.executor import GraphExecutor
from tradingagents.agent_harness.runtime.graph import GraphSpec, Edge
from tradingagents.agent_harness.runtime.nodes import ToolNode
from tradingagents.agent_harness.runtime.state import GraphState

def _graph_two_tool_nodes():
    n1 = ToolNode(id="a", agent_id="data_agent", tool_name="get_quote", raw_args={"symbol": "X"})
    n2 = ToolNode(id="b", agent_id="data_agent", tool_name="get_fundamentals", raw_args={"symbol": "X"})
    return GraphSpec(
        nodes={"a": n1, "b": n2},
        edges=[Edge(src="a", dst="b", kind="data")],
        entry="a", exit="b",
    )

async def test_executor_walks_linear_graph(monkeypatch):
    async def fake_invoke(tool_name, args, session_id):
        return {"echoed": tool_name, "args": args}
    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.nodes.invoke_tool",
        fake_invoke,
    )
    state = GraphState(run_id="r", turn_id="t", intent="x")
    out = await GraphExecutor().run(_graph_two_tool_nodes(), state)
    assert state.message_log
    assert out.intent == "x"

async def test_executor_budget_guard():
    state = GraphState(run_id="r", turn_id="t", intent="x", llm_used=5, budget_limit=5)
    state.hops_remaining = 8
    out = await GraphExecutor().run(_graph_two_tool_nodes(), state)
    assert out.llm_used == 5
```

- [ ] **Step 8.2: Run, expect FAIL**

- [ ] **Step 8.3: Implement `executor.py`**

```python
# tradingagents/agent_harness/runtime/executor.py
"""Phase 1: GraphExecutor skeleton.

Walks a GraphSpec using a priority queue. Honours budget + hop guards.
Phase 1 fully implements `ToolNode`; other kinds raise NotImplementedError
which the executor catches + logs as a warning so the graph still finishes.
"""
from __future__ import annotations
import heapq
import logging
from dataclasses import dataclass, field

from .graph import GraphSpec, Edge
from .nodes import ToolNode, LLMNode, SubplanNode, ConsultNode
from .state import GraphState, Message, TypedResult

LOGGER = logging.getLogger(__name__)

# Priority by node kind (lower = earlier)
_PRIORITY = {"verifier": 0, "llm": 1, "tool": 2, "consult": 3, "subplan": 4}

@dataclass(order=True)
class _Pending:
    priority: int
    seq: int
    msg: Message = field(compare=False)

class GraphExecutor:
    def __init__(self, *, max_hops: int = 8, llm_budget: int = 5,
                 consultation_rate_limit: float = 0.5) -> None:
        self.max_hops = max_hops
        self.llm_budget = llm_budget
        self.consultation_rate_limit = consultation_rate_limit
        self._seq = 0

    async def run(self, spec: GraphSpec, state: GraphState) -> GraphState:
        state.hops_remaining = min(state.hops_remaining, self.max_hops)
        state.budget_limit = min(state.budget_limit, self.llm_budget)

        entry = spec.entry or (next(iter(spec.nodes)) if spec.nodes else None)
        if entry is None:
            return state

        queue: list[_Pending] = []
        self._enqueue(queue, entry, Message(
            sender="__start__", receiver=entry,
            payload=TypedResult(schema=dict, data={}, meta={}),
        ))

        activated = set()
        while queue and state.hops_remaining > 0 and state.llm_used < state.budget_limit:
            item = heapq.heappop(queue)
            msg = item.msg
            if msg.receiver not in spec.nodes:
                continue
            if msg.receiver in activated and not any(
                e.src == msg.receiver and e.kind == "loop" for e in spec.edges
            ):
                continue
            activated.add(msg.receiver)

            node = spec.node(msg.receiver)
            inbox = state.consume_inbox(msg.receiver)

            try:
                out_messages = await self._invoke(node, state, inbox)
            except NotImplementedError as exc:
                LOGGER.warning("node %s not implemented in phase 1: %s", node.id, exc)
                continue
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.exception("node %s failed: %s", node.id, exc)
                continue

            for m in out_messages:
                state.message_log.append(m)
                for edge in spec.edges_from(node.id):
                    if not self._edge_fires(edge, state, m):
                        continue
                    self._enqueue(queue, edge.dst, Message(
                        sender=node.id, receiver=edge.dst,
                        payload=m.payload, kind=m.kind, hop=m.hop + 1,
                    ))
                    if edge.kind == "loop" and state.hops_remaining > 0:
                        state.hops_remaining -= 1
                        self._enqueue(queue, edge.src, Message(
                            sender=node.id, receiver=edge.src,
                            payload=m.payload, kind=m.kind, hop=m.hop + 1,
                        ))
            state.hops_remaining -= 1

        return state

    async def _invoke(self, node, state: GraphState, inbox: list[Message]) -> list[Message]:
        if isinstance(node, ToolNode):
            return await node.run(state, inbox)
        if isinstance(node, LLMNode):
            state.llm_used += 1
            return await node.run(state, inbox)
        if isinstance(node, ConsultNode):
            state.consultation_used += 1
            return await node.run(state, inbox)
        if isinstance(node, SubplanNode):
            return await node.run(state, inbox)
        raise NotImplementedError(f"unknown node kind: {type(node).__name__}")

    def _edge_fires(self, edge: Edge, state: GraphState, msg: Message) -> bool:
        if edge.kind == "data":
            return True
        if edge.kind == "when":
            if edge.predicate is None:
                return False
            try:
                return bool(edge.predicate(state, msg))
            except Exception:
                return False
        if edge.kind == "loop":
            return msg.hop < edge.max_hops
        return False

    def _enqueue(self, queue: list[_Pending], receiver: str, msg: Message) -> None:
        prio = _PRIORITY.get(msg.kind, 5)
        self._seq += 1
        heapq.heappush(queue, _Pending(priority=prio, seq=self._seq, msg=msg))
```

- [ ] **Step 8.4: Run, expect PASS**

- [ ] **Step 8.5: Commit**

```bash
git add tradingagents/agent_harness/runtime/executor.py \
        tests/agent_harness/runtime/test_executor.py
git commit -m "feat(runtime): §0.4.35 phase 1 — GraphExecutor skeleton + budget guard"
```

---

## Chunk 4: Tool Stub + Orchestrator Wiring

### Task 9: `consult_subagent` tool stub

**Files:**
- Create: `tradingagents/agent_harness/tools/builtin_consult.py`
- Test: `tests/agent_harness/tools/test_builtin_consult.py`

- [ ] **Step 9.1: Locate the tool registry**

```bash
rg -n "register_tool|@tool_registry|builtin\\.py" tradingagents/agent_harness/tools/ | head
```

- [ ] **Step 9.2: Write failing tests**

```python
# tests/agent_harness/tools/test_builtin_consult.py
import asyncio
import pytest
from tradingagents.agent_harness.tools.builtin_consult import (
    ConsultSubagentArgs, consult_subagent,
)

def test_consult_args_validation():
    args = ConsultSubagentArgs(target_agent="data_agent", question="why?")
    assert args.target_agent == "data_agent"
    assert args.max_tokens == 512
    with pytest.raises(Exception):
        ConsultSubagentArgs(target_agent="", question="x")

def test_consult_subagent_stub_raises():
    with pytest.raises(NotImplementedError):
        asyncio.run(consult_subagent(
            ConsultSubagentArgs(target_agent="data_agent", question="hi"),
            ctx={"run_id": "r", "turn_id": "t"},
        ))
```

- [ ] **Step 9.3: Run, expect FAIL**

- [ ] **Step 9.4: Implement `builtin_consult.py`**

```python
# tradingagents/agent_harness/tools/builtin_consult.py
"""Stub for `consult_subagent` — Phase 2 will wire the real LLM call."""
from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field

class ConsultSubagentArgs(BaseModel):
    target_agent: str = Field(min_length=1)
    question: str = Field(min_length=1)
    context_refs: list[str] = Field(default_factory=list)
    max_tokens: int = Field(default=512, ge=64, le=2048)

async def consult_subagent(args: ConsultSubagentArgs, ctx: dict[str, Any]) -> dict[str, Any]:
    """Phase 1: surface only. Real LLM call lands in Phase 2."""
    raise NotImplementedError(
        "consult_subagent lands in §0.4.35 phase 2 "
        f"(asked target={args.target_agent!r})"
    )
```

- [ ] **Step 9.5: Run, expect PASS**

- [ ] **Step 9.6: Register the tool stub**

Use the registration pattern discovered in Step 9.1. Typical:

```python
from .builtin_consult import consult_subagent, ConsultSubagentArgs
register_tool(
    name="consult_subagent",
    fn=consult_subagent,
    args_model=ConsultSubagentArgs,
    description="Ask another registered subagent a focused question (read-only, no recursion).",
)
```

- [ ] **Step 9.7: Commit**

```bash
git add tradingagents/agent_harness/tools/builtin_consult.py \
        tests/agent_harness/tools/test_builtin_consult.py
git commit -m "feat(runtime): §0.4.35 phase 1 — consult_subagent tool stub + registry entry"
```

### Task 10: Orchestrator wiring (flag-gated, PTC untouched)

**Files:**
- Modify: `tradingagents/agent_harness/core/orchestrator.py`

- [ ] **Step 10.1: Locate dispatch site**

```bash
rg -n "PTCExecutor|ptc\\.run|dispatch_plan|asyncio\\.gather" tradingagents/agent_harness/core/orchestrator.py | head
```

- [ ] **Step 10.2: Insert runtime branch (no behaviour change in OFF state)**

Add a helper near the top of `orchestrator.py`:

```python
from ..runtime import GraphExecutor, PlanCompiler, RuntimeSettings
from ..runtime.compiler import CompileError

_runtime_settings = RuntimeSettings()  # reads live config in step 10.3
```

At the dispatch site:

```python
if _runtime_settings.multi_agent:
    compiler = PlanCompiler()
    try:
        compiled = compiler.compile(plan)
    except CompileError as exc:
        log.warning("graph compile failed, falling back to PTC: %s", exc)
    else:
        executor = GraphExecutor(
            max_hops=_runtime_settings.max_hops,
            llm_budget=_runtime_settings.llm_budget_per_turn,
            consultation_rate_limit=_runtime_settings.consultation_rate_limit,
        )
        graph_state = _build_initial_state(plan, run_id=..., turn_id=...)
        await executor.run(compiled, graph_state)
        return _post_state(graph_state)
# else: fall through to PTC (unchanged)
```

- [ ] **Step 10.3: Verify default OFF preserves behaviour**

```bash
.venv/bin/python -m pytest tests/ -x -q
```

Expected: all green (PTC path is the only one taken when `multi_agent=false`).

- [ ] **Step 10.4: Smoke-test the ON path (do NOT commit)**

```yaml
# live settings YAML
runtime:
  multi_agent: true
```

Reload service, send a chat, tail `/tmp/tradingagents-web-venv.log` for `GraphExecutor.run` lines. Reset to `false` afterwards.

- [ ] **Step 10.5: Commit**

```bash
git add tradingagents/agent_harness/core/orchestrator.py
git commit -m "feat(runtime): §0.4.35 phase 1 — flag-gated GraphExecutor dispatch (default off)"
```

### Task 11: End-to-end smoke test

**Files:** none (uses existing harness UI)

- [ ] **Step 11.1: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 11.2: Manual sanity**

- Start the service: `launchctl kickstart -k "gui/$(id -u)/com.tradingagents.web.venv"`
- Open `http://127.0.0.1:8000/`
- Send a chat; confirm `/reports` and `/scheduled` lists still render (`8f3815f` fix must not regress).
- Tail `/tmp/tradingagents-web-venv.log` for new stack traces mentioning `agent_harness.runtime`.

- [ ] **Step 11.3: Tag the phase**

```bash
git tag -a phase1-runtime-skeleton -m "§0.4.35 phase 1 — types + executor skeleton landed"
git push tradingagentsplus phase1-runtime-skeleton
```

---

## Acceptance Checklist (Phase 1)

- [ ] `RuntimeSettings` defaults match spec §4.9 (`multi_agent=false`, `llm_budget_per_turn=5`, `max_hops=8`, `consultation_rate_limit=0.5`)
- [ ] `GraphState` / `TypedResult` / `Message` / `FieldRef` importable from `tradingagents.agent_harness.runtime`
- [ ] `GraphSpec` validates nodes + edges; serializes round-trip
- [ ] `resolve_ref()` handles `$agent.field`, `* N`, `? a : b`; falls back to literal on failure
- [ ] `PlanCompiler` produces a `GraphSpec` for the simple flat-parallel case
- [ ] `GraphExecutor.run()` walks a 2-node linear graph with `ToolNode` and respects budget
- [ ] `consult_subagent` tool is registered; `NotImplementedError` body until Phase 2
- [ ] Default flag OFF → PTC path identical to before
- [ ] All existing tests pass (`pytest tests/ -q`)
- [ ] `/reports` and `/scheduled` pages still render (regression guard)
- [ ] Phase 1 tag pushed to `tradingagentsplus`

## Out of Scope (deferred to later phases)

- Phase 2: real LLM call in `LLMNode` + `ConsultNode`; budget enforcement
- Phase 3: cross-agent `$ref` resolution at runtime (not just parser tests)
- Phase 4: `SubplanNode.run` + `trading_agents` graph fragment wired in
- Phase 5: cutover — `multi_agent=true` becomes default
- Phase 6: mid-flight `GraphState` snapshots via `AgentRuntimeStore`
