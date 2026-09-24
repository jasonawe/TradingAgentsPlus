# [Phase 1 — Multi-Agent Runtime Skeleton] Implementation Plan (rev. 3)

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the type system, `$ref` resolver (with proper operator precedence), `PlanCompiler` (group-order case), `GraphExecutor` skeleton (with inbox plumbing), and `consult_subagent` stub **behind the `runtime.multi_agent` flag (default off)** without changing any existing behaviour. All current tests must remain green.

**Architecture:** All new code lives under a NEW sub-package `tradingagents/agent_harness/runtime/multi_agent/` to avoid colliding with the existing supervised AgentRuntime module (`AgentRuntime`, `AgentRuntimeStore`, `PlanGraph`, `GraphPatch`, etc.). The orchestrator gains a new code path gated by `runtime.multi_agent`; the existing PTC branch is untouched in this phase.

**Critical Phase 1 contract** (locked in rev. 3 after round-2 review): the executor MUST populate `state.inbox` for downstream nodes whenever it fires an edge. Without this, the spec §4.7 "data flow between subagents" goal is hollow and Phase 3's `$ref` resolver would have nothing to read. The executor calls `state.append_inbox(new_msg)` for every edge fire — this both appends to the audit log and deposits the message into the receiver's inbox for the next activation.

**Relationship to existing PlanGraph** (resolves review blocker F2):
- `PlanGraph` (existing, supervised V2 runtime): run-level DAG — Tasks, dependencies, lifecycle states, persistence. Long-lived (hours to days).
- `GraphSpec` (new, Phase 1): turn-level ephemeral message-passing graph for multi-agent coordination within a single turn. Short-lived (seconds).
- The two are **complementary, not competing**. Phase 6 will persist mid-flight `GraphState` snapshots into the existing `AgentRuntimeStore` for crash recovery; Phase 1 only persists in memory.

**Tech Stack:** Python 3.14.6, asyncio, dataclasses (NOT Pydantic for GraphSpec/Edge — see H4 below), `pytest>=8.0` with `asyncio.run()` wrapper (no `pytest-asyncio` available in this codebase; existing tests use a `_run()` helper).

**Spec:** `docs/superpowers/specs/2026-09-24-multi-agent-message-passing-runtime.md`

**Locked decisions (spec §8):**
- Q1=A declared inputs only (cross-agent access via `$ref` + edge)
- Q2=A re-execute upstream node on loop trigger (hop index for staleness)
- Q3=B consultation cap configurable 0.0–0.8 (default 0.5)
- Q4=B all agents (`data`/`alpha`/`news`/`trading_agents`) nestable
- Q5=B mid-flight state persisted via `AgentRuntimeStore` (lands in Phase 6)

---

## File Structure (revised to avoid collisions)

### New files (all under `tradingagents/agent_harness/runtime/multi_agent/`)

| File | Lines | Responsibility |
|---|---|---|
| `__init__.py` | ~30 | Public exports: `GraphSpec`, `Edge`, `NodeKind`, `BaseNode`, `GraphState`, `TypedResult`, `Message`, `FieldRef`, `ToolNode`, `LLMNode`, `SubplanNode`, `ConsultNode`, `GraphExecutor`, `PlanCompiler`, `CompileError`, `RuntimeSettings`, `load_settings` |
| `settings.py` | ~30 | `RuntimeSettings` (Pydantic BaseModel) + `load_settings()` factory that reads from `tradingagents.default_config` |
| `state.py` | ~120 | `GraphState`, `TypedResult`, `Message`, `FieldRef` (dataclasses). `append_inbox` is the canonical way to deposit a message into a receiver's inbox (it also appends to `message_log`). |
| `graph.py` | ~140 | `NodeKind` (Enum), `BaseNode` (Protocol), `Edge` (dataclass + `__post_init__` validation), `GraphSpec` (dataclass + custom `to_dict()`) |
| `resolver.py` | ~180 | `parse_ref()`, `is_ref_expr()`, `resolve_ref()` (pure functions, **rightmost-split precedence** for multi-op expressions) |
| `nodes.py` | ~200 | `ToolNode` (uses `ToolPipeline.run` correctly) + `LLMNode`/`SubplanNode`/`ConsultNode` stubs. **No `_NodeBase` indirection** — subclasses set attributes directly. |
| `compiler.py` | ~180 | `PlanCompiler` (compiles `RouterPlan` → `GraphSpec`); reads cross-group deps from group order (NOT per-call `depends_on`, which doesn't exist on `ToolCall` per B5) |
| `executor.py` | ~280 | `GraphExecutor` (priority queue + budget guard + **inbox plumbing**); swallows `NotImplementedError` for not-yet-implemented node kinds; emits INFO heartbeats so smoke tests can verify execution |

### Modified files

| File | Change |
|---|---|
| `tradingagents/default_config.py` | (a) Add `runtime.multi_agent=false`, `runtime.llm_budget_per_turn=5`, `runtime.max_hops=8`, `runtime.consultation_rate_limit=0.5` to `DEFAULT_CONFIG`. (b) Add 4 new entries to `_ENV_OVERRIDES` (`TRADINGAGENTS_RUNTIME_MULTI_AGENT` etc.). (c) Add `_set_dotted(cfg, dotted_key, value)` helper (10 lines). (d) Update `_apply_env_overrides` to call `_set_dotted` so dotted keys walk the config tree. |
| `tradingagents/agent_harness/core/orchestrator.py` | Add `if _settings.multi_agent:` branch that calls `PlanCompiler.compile()` then `GraphExecutor.run()`; fall through to PTC on `CompileError` or any `Exception` during graph execution. The fragment drops the redundant `_program = _plan_from_router(...)` call (already happens upstream at line 2247). |

### New tool (separate file)

| File | Lines | Responsibility |
|---|---|---|
| `tradingagents/agent_harness/tools/builtin_consult.py` | ~80 | `ConsultSubagentArgs` (Pydantic) + `consult_subagent` async fn. **Critical**: second param is named `context` (NOT `ctx`) so `FunctionTool.invoke` (`tools/base.py:91-95`) auto-injects the tool context. The stub raises `NotImplementedError`. Phase 2 wires registration via `@tool_registry.register(...)` on the per-harness `ToolRegistry`; Phase 1 does NOT register. |

### New test files (flat in `tests/`, NOT nested)

| File | Coverage |
|---|---|
| `tests/test_multi_agent_settings.py` | `RuntimeSettings` defaults + override + **env var injection** (covers the dotted-path helper) |
| `tests/test_multi_agent_state.py` | `GraphState`/`TypedResult`/`Message`/`FieldRef` round-trip |
| `tests/test_multi_agent_graph.py` | `GraphSpec` validation + `to_dict()` round-trip + `Edge.kind` validation |
| `tests/test_multi_agent_resolver.py` | `$ref` parser + evaluator — **includes multi-op arithmetic precedence tests** |
| `tests/test_multi_agent_compiler.py` | `PlanCompiler` simple case + `_TOOL_TO_AGENT` mapping (including agent-as-tool entries like `add_to_watchlist` → `command_resolver`) |
| `tests/test_multi_agent_nodes.py` | `ToolNode` stub end-to-end via mocked `ToolPipeline.run` |
| `tests/test_multi_agent_executor.py` | `GraphExecutor.run()` walks 2-node linear graph; **inbox propagation regression**; budget guard; priority by node kind |
| `tests/test_consult_subagent.py` | `ConsultSubagentArgs` validation + stub raises `NotImplementedError` + signature uses `context=` |

All tests use the existing `asyncio.run()` + `_run(coro)` pattern from `tests/test_tool_pipeline.py:30`. **No bare `async def test_...`** (per review B4).

---

## Chunk 1: Settings + Types (no behavior change, just shape)

### Task 1: `RuntimeSettings` + config wiring (dotted-path + env var injection)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/settings.py`
- Modify: `tradingagents/default_config.py` (add `runtime` block + env overrides + `_set_dotted` helper)
- Test: `tests/test_multi_agent_settings.py`

- [ ] **Step 1.1: Write failing test (3 cases)**

```python
# tests/test_multi_agent_settings.py
import importlib

from tradingagents.agent_harness.runtime.multi_agent.settings import (
    RuntimeSettings, load_settings,
)

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

def test_load_settings_reads_from_set_config():
    # Sanity: load_settings() pulls from the live config dict via set_config.
    from tradingagents.dataflows import config as cfg
    cfg.set_config({"runtime": {"multi_agent": True, "llm_budget_per_turn": 7}})
    s = load_settings()
    assert s.multi_agent is True
    assert s.llm_budget_per_turn == 7

def test_env_override_runtime_multi_agent(monkeypatch):
    # Verify the dotted-path env var wiring lands at the right nested key.
    monkeypatch.setenv("TRADINGAGENTS_RUNTIME_MULTI_AGENT", "true")
    monkeypatch.setenv("TRADINGAGENTS_RUNTIME_LLM_BUDGET_PER_TURN", "9")
    # Re-import default_config so _apply_env_overrides runs against fresh env.
    import tradingagents.default_config as dc
    importlib.reload(dc)
    import tradingagents.dataflows.config as dfcfg
    importlib.reload(dfcfg)
    cfg = dfcfg.get_config()
    assert cfg["runtime"]["multi_agent"] is True
    assert cfg["runtime"]["llm_budget_per_turn"] == 9
```

- [ ] **Step 1.2: Run, expect FAIL**

Run: `.venv/bin/python -m pytest tests/test_multi_agent_settings.py -v`
Expected: `ModuleNotFoundError: tradingagents.agent_harness.runtime.multi_agent.settings` (first 3) + missing env override (4th).

- [ ] **Step 1.3: Implement `settings.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/settings.py
from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field

class RuntimeSettings(BaseModel):
    multi_agent: bool = False
    llm_budget_per_turn: int = Field(default=5, ge=1, le=50)
    max_hops: int = Field(default=8, ge=1, le=64)
    consultation_rate_limit: float = Field(default=0.5, ge=0.0, le=0.8)

def load_settings() -> RuntimeSettings:
    """Read runtime settings from the live dataflows config."""
    from tradingagents.dataflows.config import get_config
    cfg = get_config()
    block = cfg.get("runtime", {}) or {}
    return RuntimeSettings(**block)
```

- [ ] **Step 1.4: Add `runtime` block to `DEFAULT_CONFIG`**

At the bottom of the `DEFAULT_CONFIG` dict in `tradingagents/default_config.py`, before the closing `}`:

```python
    # §0.4.35 Phase 1 — multi-agent runtime (default off; PTC remains live path)
    "runtime": {
        "multi_agent": False,
        "llm_budget_per_turn": 5,
        "max_hops": 8,
        "consultation_rate_limit": 0.5,
    },
```

- [ ] **Step 1.5: Add env overrides + `_set_dotted` helper to `default_config.py`**

In `tradingagents/default_config.py`, add the following 4 entries to the existing `_ENV_OVERRIDES` dict (lines 11-33):

```python
    # §0.4.35 Phase 1 — dotted paths land at the runtime.* block
    "TRADINGAGENTS_RUNTIME_MULTI_AGENT":          "runtime.multi_agent",
    "TRADINGAGENTS_RUNTIME_LLM_BUDGET_PER_TURN":  "runtime.llm_budget_per_turn",
    "TRADINGAGENTS_RUNTIME_MAX_HOPS":             "runtime.max_hops",
    "TRADINGAGENTS_RUNTIME_CONSULTATION_RATE":    "runtime.consultation_rate_limit",
```

Add this helper near `_apply_env_overrides` (after `_coerce`):

```python
def _set_dotted(cfg: dict, dotted_key: str, value: Any) -> dict:
    """Walk a dotted key path and assign the leaf value.

    Example:
        >>> _set_dotted({}, "runtime.multi_agent", True)
        {"runtime": {"multi_agent": True}}
    """
    parts = dotted_key.split(".")
    cur = cfg
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value
    return cfg
```

Update `_apply_env_overrides` (replace the `config[key] = _coerce(...)` line) so it calls `_set_dotted` for dotted keys:

```python
def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            coerced = _coerce(raw, _lookup_dotted(config, key))
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
        _set_dotted(config, key, coerced)
    return config

def _lookup_dotted(cfg: dict, dotted_key: str):
    """Read a dotted key without mutating. Returns None for missing paths."""
    parts = dotted_key.split(".")
    cur: Any = cfg
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur
```

For backward compatibility: flat keys (no dot) still hit the same path; `_set_dotted({}, "alpha", 1)` returns `{"alpha": 1}` and `_lookup_dotted({}, "alpha")` returns `None`, which `_coerce` then receives as `None` — Python-level check needed: in the existing `_coerce(value, reference=None)`, all branches assume `reference` is non-None. Add a guard so flat keys where the default isn't yet in `DEFAULT_CONFIG` still coerce against a sentinel:

```python
def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        reference = _lookup_dotted(config, key)
        if reference is None:
            # No existing default — best-effort coerce against the raw string.
            reference = raw
        try:
            coerced = _coerce(raw, reference)
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
        _set_dotted(config, key, coerced)
    return config
```

- [ ] **Step 1.6: Run, expect PASS**

- [ ] **Step 1.7: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/settings.py \
        tradingagents/default_config.py \
        tests/test_multi_agent_settings.py
git commit -m "feat(runtime): §0.4.35 phase 1 — RuntimeSettings + dotted-path env overrides"
```

### Task 2: `GraphState` + `TypedResult` + `Message` + `FieldRef`

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/state.py`
- Test: `tests/test_multi_agent_state.py`

- [ ] **Step 2.1: Write failing tests**

```python
# tests/test_multi_agent_state.py
import time
from tradingagents.agent_harness.runtime.multi_agent.state import (
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

def test_graph_state_append_inbox_deposits_and_logs():
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    msg = Message(sender="x", receiver="y",
                  payload=TypedResult(schema=dict, data={"a": 1}, meta={}))
    state.append_inbox(msg)
    assert state.inbox["y"] == [msg]
    assert state.message_log == [msg]

def test_graph_state_consume_inbox_is_idempotent():
    state = GraphState(run_id="r1", turn_id="t1", intent="analysis")
    msg = Message(sender="x", receiver="y",
                  payload=TypedResult(schema=dict, data={"a": 1}, meta={}))
    state.append_inbox(msg)
    inbox = state.consume_inbox("y")
    assert len(inbox) == 1
    assert state.consume_inbox("y") == []
```

- [ ] **Step 2.2: Run, expect FAIL**

- [ ] **Step 2.3: Implement `state.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/state.py
"""Turn-level ephemeral state for the multi-agent runtime (Phase 1)."""
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
        """Deposit a message into a receiver's inbox AND the audit log.

        Per spec §4.7: this is the canonical way messages flow between nodes.
        The executor calls this for every edge fire so the next activation
        of the downstream node can ``consume_inbox(receiver)`` and see its
        upstream inputs.
        """
        self.inbox.setdefault(msg.receiver, []).append(msg)
        self.message_log.append(msg)

    def consume_inbox(self, receiver: str) -> list[Message]:
        """Pop and return all messages addressed to ``receiver``. Idempotent."""
        return self.inbox.pop(receiver, [])

    # NOTE: fork() lands in Phase 4 when SubplanNode needs it.
```

- [ ] **Step 2.4: Run, expect PASS**

- [ ] **Step 2.5: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/state.py \
        tests/test_multi_agent_state.py
git commit -m "feat(runtime): §0.4.35 phase 1 — GraphState, TypedResult, Message, FieldRef"
```

### Task 3: `NodeKind` + `BaseNode` + `Edge` (with validation) + `GraphSpec`

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/graph.py`
- Test: `tests/test_multi_agent_graph.py`

- [ ] **Step 3.1: Write failing tests**

```python
# tests/test_multi_agent_graph.py
import pytest
from tradingagents.agent_harness.runtime.multi_agent.graph import (
    NodeKind, Edge, GraphSpec, BaseNode,
)

def test_node_kind_values():
    assert NodeKind.TOOL.value == "tool"
    assert NodeKind.LLM.value == "llm"
    assert NodeKind.SUBPLAN.value == "subplan"
    assert NodeKind.CONSULT.value == "consult"

def test_edge_validates_kind():
    with pytest.raises(ValueError, match="invalid edge kind"):
        Edge(src="a", dst="b", kind="bogus")  # type: ignore[arg-type]

def test_edge_accepts_valid_kinds():
    for k in ("data", "when", "loop"):
        e = Edge(src="a", dst="b", kind=k)  # type: ignore[arg-type]
        assert e.kind == k

class _StubNode:
    id = "n1"
    agent_id = "data_agent"
    kind = NodeKind.TOOL

    def __init__(self):
        from tradingagents.agent_harness.runtime.multi_agent.state import (
            FieldRef,
        )
        self.inputs: list[FieldRef] = []
        self.outputs: list[FieldRef] = []

    async def run(self, state, inbox):
        return []

def test_graph_spec_node_lookup():
    g = GraphSpec(nodes={"n1": _StubNode()}, edges=[])
    assert g.node("n1").id == "n1"

def test_graph_spec_to_dict_roundtrip():
    g = GraphSpec(
        nodes={"n1": _StubNode()},
        edges=[Edge(src="n1", dst="n2", kind="data")],
        entry="n1", exit="n2",
    )
    blob = g.to_dict()
    assert blob["entry"] == "n1"
    assert blob["exit"] == "n2"
    assert "n1" in blob["nodes"]
    assert any(e["kind"] == "data" for e in blob["edges"])
```

- [ ] **Step 3.2: Run, expect FAIL**

- [ ] **Step 3.3: Implement `graph.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/graph.py
"""Core graph types for the multi-agent runtime (Phase 1).

Dataclasses only — Pydantic was avoided because Pydantic can't serialize
Protocol-typed fields cleanly (see review H4). Edge validates `kind` in
__post_init__ (see review H3).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable
import json

from .state import FieldRef, Message, TypedResult

class NodeKind(str, Enum):
    TOOL = "tool"
    LLM = "llm"
    SUBPLAN = "subplan"
    CONSULT = "consult"

_VALID_EDGE_KINDS = ("data", "when", "loop")

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
    kind: str
    field_ref: Optional[str] = None
    predicate: Optional[Any] = None
    max_hops: int = 3

    def __post_init__(self) -> None:
        if self.kind not in _VALID_EDGE_KINDS:
            raise ValueError(
                f"invalid edge kind: {self.kind!r} (must be one of {_VALID_EDGE_KINDS})"
            )

@dataclass
class GraphSpec:
    """Turn-level graph for multi-agent message passing.

    `nodes` is a dict of any object satisfying the BaseNode Protocol (duck-typed).
    `edges` is a list of Edge. Phase 1 deliberately stays dataclass-based for
    clean serialization via `to_dict()`.
    """
    nodes: dict[str, BaseNode]
    edges: list[Edge] = field(default_factory=list)
    entry: Optional[str] = None
    exit: Optional[str] = None

    def node(self, node_id: str) -> BaseNode:
        return self.nodes[node_id]

    def edges_from(self, src: str) -> list[Edge]:
        return [e for e in self.edges if e.src == src]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable snapshot (Phase 1: cache key + logging)."""
        return {
            "nodes": {
                nid: {"id": n.id, "agent_id": n.agent_id, "kind": n.kind.value}
                for nid, n in self.nodes.items()
            },
            "edges": [
                {"src": e.src, "dst": e.dst, "kind": e.kind,
                 "field_ref": e.field_ref, "max_hops": e.max_hops}
                for e in self.edges
            ],
            "entry": self.entry,
            "exit": self.exit,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
```

- [ ] **Step 3.4: Run, expect PASS**

- [ ] **Step 3.5: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/graph.py \
        tests/test_multi_agent_graph.py
git commit -m "feat(runtime): §0.4.35 phase 1 — NodeKind, Edge (validated), GraphSpec"
```

### Task 4: `__init__.py` partial exports

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/__init__.py`

- [ ] **Step 4.1: Write public exports**

```python
# tradingagents/agent_harness/runtime/multi_agent/__init__.py
from .graph import NodeKind, Edge, GraphSpec, BaseNode
from .state import GraphState, TypedResult, Message, FieldRef
from .settings import RuntimeSettings, load_settings
# Nodes + executor + compiler are added in later tasks.

__all__ = [
    "NodeKind", "Edge", "GraphSpec", "BaseNode",
    "GraphState", "TypedResult", "Message", "FieldRef",
    "RuntimeSettings", "load_settings",
]
```

- [ ] **Step 4.2: Verify import**

Run: `.venv/bin/python -c "from tradingagents.agent_harness.runtime.multi_agent import GraphSpec, RuntimeSettings, load_settings; print(GraphSpec, RuntimeSettings, load_settings())"`
Expected: prints the three objects, no error.

- [ ] **Step 4.3: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/__init__.py
git commit -m "feat(runtime): §0.4.35 phase 1 — multi_agent package __init__ (partial exports)"
```

---

## Chunk 2: Resolver + consult_subagent Stub

### Task 5: `$ref` parser + evaluator (rightmost-split precedence)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/resolver.py`
- Test: `tests/test_multi_agent_resolver.py`

- [ ] **Step 5.1: Write failing tests — including multi-op precedence**

```python
# tests/test_multi_agent_resolver.py
from tradingagents.agent_harness.runtime.multi_agent.resolver import (
    parse_ref, is_ref_expr, resolve_ref,
)
from tradingagents.agent_harness.runtime.multi_agent.state import (
    GraphState, TypedResult,
)

def _state():
    return GraphState(
        run_id="r", turn_id="t", intent="x",
        agent_outputs={
            "data": TypedResult(
                schema=dict,
                data={"x": 2, "y": 3, "q": {"symbol": "600036.SS"}, "tag": "buy"},
                meta={},
            ),
            "alpha": TypedResult(schema=dict, data={"price": 40.5}, meta={}),
        },
    )

def test_parse_ref_basic():
    assert parse_ref("\$data.quote.symbol") == ("data", "quote.symbol")

def test_parse_ref_no_dollar_returns_none():
    assert parse_ref("data.quote.symbol") is None

def test_is_ref_expr():
    assert is_ref_expr("\$data.x")
    assert not is_ref_expr("hello")
    assert is_ref_expr("\$data.x * 1.5")

def test_resolve_ref_simple():
    s = _state()
    assert resolve_ref("\$data.q.symbol", s) == "600036.SS"

def test_resolve_ref_nested_dict():
    s = _state()
    assert resolve_ref("\$data.q.symbol", s) == "600036.SS"

def test_resolve_ref_arithmetic_single_op():
    s = _state()
    assert resolve_ref("\$alpha.price * 2", s) == 81.0
    assert resolve_ref("\$alpha.price * 1.5", s) == 60.75

def test_resolve_ref_arithmetic_precedence_two_refs_with_constant():
    # Critical rev.3 test — earlier resolver got this WRONG
    # (x * (2 + y) * 3 == 60 for x=2, y=3; correct is x*2 + y*3 == 13).
    s = _state()
    assert resolve_ref("\$data.x * 2 + \$data.y * 3", s) == 13

def test_resolve_ref_arithmetic_precedence_left_associative():
    s = _state()
    # 2 + 3 * 4 == 14 (not 20)
    assert resolve_ref("\$data.x + \$data.y * 4", s) == 14
    # (2 + 3) * 4 == 20
    assert resolve_ref("(\$data.x + \$data.y) * 4", s) == 20

def test_resolve_ref_ternary_truthy():
    s = _state()
    assert resolve_ref("\$alpha.price > 40 ? 'expensive' : 'cheap'", s) == "expensive"

def test_resolve_ref_arithmetic_in_ternary():
    s = _state()
    # (\$data.x * 2 == 4) ? 'four' : 'other'
    assert resolve_ref("\$data.x * 2 == 4 ? 'four' : 'other'", s) == "four"

def test_resolve_ref_falls_back_to_literal():
    s = _state()
    assert resolve_ref("\$nope.field", s, literal="fallback") == "fallback"

def test_resolve_ref_passes_through_non_string():
    s = _state()
    assert resolve_ref(42, s) == 42
```

- [ ] **Step 5.2: Run, expect FAIL** (the precedence test will fail with rev. 2 logic)

- [ ] **Step 5.3: Implement `resolver.py` with rightmost-split precedence**

```python
# tradingagents/agent_harness/runtime/multi_agent/resolver.py
"""Pure \$ref parser + evaluator for the multi-agent runtime (Phase 1).

Operator precedence is handled by **rightmost-split**: we iterate operators
from LOWEST precedence to HIGHEST, and at each level find the RIGHTMOST
occurrence. This gives left-to-right associativity with correct precedence:
``\$data.x * 2 + \$data.y * 3`` parses as ``(\$data.x * 2) + (\$data.y * 3)``,
not ``\$data.x * (2 + \$data.y) * 3``.
"""
from __future__ import annotations
import re
from typing import Any, Optional

from .state import GraphState

_REF_RE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z0-9_.]+)")

def is_ref_expr(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return "$" in value and bool(_REF_RE.search(value))

def parse_ref(value: str) -> Optional[tuple[str, str]]:
    if not isinstance(value, str) or not value.startswith("$"):
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

def _truthy(v: Any) -> bool:
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)

def _try_split(value: str, op: str):
    """Find the rightmost occurrence of op and split there. Returns (left, right) or None."""
    idx = value.rfind(op)
    if idx < 0:
        return None
    return value[:idx], value[idx + len(op):]

def _eval_expr(value: str, state: GraphState) -> Any:
    # Ternary (lowest precedence in our surface)
    if "?" in value and ":" in value:
        cond, rest = value.split("?", 1)
        a, b = rest.split(":", 1)
        cond_val = _eval_expr(cond.strip(), state)
        chosen = a if _truthy(cond_val) else b
        return _eval_expr(chosen.strip(), state)

    # Arithmetic — process by precedence (low to high), rightmost split
    for ops in [
        # Level 1: addition / subtraction (lowest)
        [(" + ", lambda a, b: a + b),
         (" - ", lambda a, b: a - b)],
        # Level 2: multiplication / division
        [(" * ", lambda a, b: a * b),
         (" / ", lambda a, b: a / b)],
    ]:
        for op, fn in ops:
            split = _try_split(value, op)
            if split is None:
                continue
            left, right = split
            return fn(_eval_expr(left.strip(), state),
                      _eval_expr(right.strip(), state))

    # Comparison (higher precedence than arithmetic; lower than ref-lookup below)
    for op, fn in ((" >= ", lambda a, b: a >= b),
                   (" <= ", lambda a, b: a <= b),
                   (" == ", lambda a, b: a == b),
                   (" != ", lambda a, b: a != b),
                   (" > ", lambda a, b: a > b),
                   (" < ", lambda a, b: a < b)):
        split = _try_split(value, op)
        if split is None:
            continue
        left, right = split
        return fn(_eval_expr(left.strip(), state),
                  _eval_expr(right.strip(), state))

    # Plain $ref
    ref = parse_ref(value.strip())
    if ref:
        agent, dotted = ref
        return _lookup(state, agent, dotted)

    # Literal (int / float / string)
    stripped = value.strip()
    try:
        return int(stripped)
    except ValueError:
        try:
            return float(stripped)
        except ValueError:
            if stripped.startswith('"') and stripped.endswith('"'):
                return stripped[1:-1]
            return stripped

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
git add tradingagents/agent_harness/runtime/multi_agent/resolver.py \
        tests/test_multi_agent_resolver.py
git commit -m "feat(runtime): §0.4.35 phase 1 — \$ref parser/evaluator (rightmost-split precedence)"
```

### Task 6: `consult_subagent` tool stub (param named `context`, NO registration)

**Files:**
- Create: `tradingagents/agent_harness/tools/builtin_consult.py`
- Test: `tests/test_consult_subagent.py`

> **Phase 1 does NOT register the tool** — registration lands in Phase 2 via `@tool_registry.register(...)` on the per-harness `ToolRegistry` instance (`Harness.__init__` calls `install_builtin_tools(self.tool_registry)`). **Critical rev.3 fix**: the second param is named `context`, NOT `ctx`, because `FunctionTool.invoke` (`tools/base.py:91-95`) only auto-injects the tool context when the param is literally named `"context"`. Getting this wrong now means Phase 2 has to rebuild the contract.

- [ ] **Step 6.1: Write failing tests**

```python
# tests/test_consult_subagent.py
import asyncio
import inspect
import pytest
from tradingagents.agent_harness.tools.builtin_consult import (
    ConsultSubagentArgs, consult_subagent,
)

def test_consult_args_validation():
    args = ConsultSubagentArgs(target_agent="data_agent", question="why?")
    assert args.target_agent == "data_agent"
    assert args.max_tokens == 512
    with pytest.raises(Exception):
        ConsultSubagentArgs(target_agent="", question="x")  # min_length=1

def test_consult_subagent_signature_uses_context_param():
    # Phase 2 hazard guard: FunctionTool.invoke auto-injects context only
    # when the param is literally named "context".
    sig = inspect.signature(consult_subagent)
    params = list(sig.parameters.keys())
    assert "context" in params, (
        f"FunctionTool.invoke only auto-injects the tool context when the "
        f"param name is 'context' (see tools/base.py:91-95); got {params!r}"
    )

def test_consult_subagent_stub_raises():
    args = ConsultSubagentArgs(target_agent="data_agent", question="hi")
    with pytest.raises(NotImplementedError):
        asyncio.run(consult_subagent(args, context={"run_id": "r", "turn_id": "t"}))
```

- [ ] **Step 6.2: Run, expect FAIL**

- [ ] **Step 6.3: Implement `builtin_consult.py`**

```python
# tradingagents/agent_harness/tools/builtin_consult.py
"""Stub for `consult_subagent` — Phase 2 wires registration + real LLM call.

Phase 1 surfaces the contract only. Registration happens in Phase 2 via
``@tool_registry.register(...)`` on the per-harness ``ToolRegistry``
instance (see ``Harness.__init__`` which calls ``install_builtin_tools``).

CRITICAL: the second param is named ``context`` (not ``ctx``) because
``FunctionTool.invoke`` (``tradingagents/agent_harness/tools/base.py:91-95``)
only auto-injects the tool context when the param is literally named
``"context"``. If we rename later, every caller will break.
"""
from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field

class ConsultSubagentArgs(BaseModel):
    target_agent: str = Field(min_length=1)
    question: str = Field(min_length=1)
    context_refs: list[str] = Field(default_factory=list)
    max_tokens: int = Field(default=512, ge=64, le=2048)

async def consult_subagent(args: ConsultSubagentArgs, context: dict[str, Any]) -> dict[str, Any]:
    """Phase 1: surface only. Real LLM call lands in Phase 2."""
    raise NotImplementedError(
        "consult_subagent lands in §0.4.35 phase 2 "
        f"(asked target={args.target_agent!r})"
    )
```

- [ ] **Step 6.4: Run, expect PASS**

- [ ] **Step 6.5: Commit**

```bash
git add tradingagents/agent_harness/tools/builtin_consult.py \
        tests/test_consult_subagent.py
git commit -m "feat(runtime): §0.4.35 phase 1 — consult_subagent stub (context-named param)"
```

---

## Chunk 3: Nodes + PlanCompiler

### Task 7: `ToolNode` + stub nodes (no `_NodeBase` indirection)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/nodes.py`
- Test: `tests/test_multi_agent_nodes.py`

> **No `_NodeBase` parallel hierarchy** (rev.3 fix from review L12): subclasses set `id / agent_id / kind / inputs / outputs` directly in their `__init__`. Drops ~30 lines.

- [ ] **Step 7.1: Verify ToolPipeline call signature**

```bash
rg -n "async def run\\(" tradingagents/agent_harness/tools/pipeline.py
```

Expected: `pipeline.run(tool_name=..., args=..., tool_context=..., executor=...)` returning a `PipelineResult` with `ok / result / error` attrs.

- [ ] **Step 7.2: Write failing tests**

```python
# tests/test_multi_agent_nodes.py
import asyncio
from unittest.mock import MagicMock

from tradingagents.agent_harness.runtime.multi_agent.nodes import (
    ToolNode, LLMNode, SubplanNode, ConsultNode,
)
from tradingagents.agent_harness.runtime.multi_agent.graph import NodeKind
from tradingagents.agent_harness.runtime.multi_agent.state import GraphState

async def _run(coro):
    return await coro

def test_node_kind_attributes():
    assert ToolNode(id="a", agent_id="data_agent", tool_name="get_quote",
                    raw_args={"symbol": "X"}).kind == NodeKind.TOOL
    assert LLMNode(id="b", agent_id="data_agent",
                   system_prompt="...").kind == NodeKind.LLM
    assert SubplanNode(id="c", agent_id="trading_agents",
                       sub_graph=MagicMock()).kind == NodeKind.SUBPLAN
    assert ConsultNode(id="d", agent_id="data_agent",
                       target_agent="alpha_agent",
                       question="?").kind == NodeKind.CONSULT

def test_tool_node_dispatches_via_pipeline(monkeypatch):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    captured = {}

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            captured["tool_name"] = tool_name
            captured["args"] = args
            return MagicMock(ok=True, result={"echoed": tool_name, "args": args})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    node = ToolNode(id="a", agent_id="data_agent",
                    tool_name="get_quote", raw_args={"symbol": "X"})
    state = GraphState(run_id="r", turn_id="t", intent="x")
    out = _run(node.run(state, inbox=[]))
    assert captured["tool_name"] == "get_quote"
    assert captured["args"] == {"symbol": "X"}
    assert len(out) == 1
    assert out[0].payload.data == {"echoed": "get_quote", "args": {"symbol": "X"}}

def test_llm_node_raises_not_implemented():
    node = LLMNode(id="b", agent_id="data_agent", system_prompt="...")
    state = GraphState(run_id="r", turn_id="t", intent="x")
    import pytest
    with pytest.raises(NotImplementedError):
        _run(node.run(state, inbox=[]))

def test_subplan_node_raises_not_implemented():
    node = SubplanNode(id="c", agent_id="trading_agents", sub_graph=MagicMock())
    state = GraphState(run_id="r", turn_id="t", intent="x")
    import pytest
    with pytest.raises(NotImplementedError):
        _run(node.run(state, inbox=[]))

def test_consult_node_raises_not_implemented():
    node = ConsultNode(id="d", agent_id="data_agent",
                       target_agent="alpha_agent", question="?")
    state = GraphState(run_id="r", turn_id="t", intent="x")
    import pytest
    with pytest.raises(NotImplementedError):
        _run(node.run(state, inbox=[]))
```

- [ ] **Step 7.3: Run, expect FAIL**

- [ ] **Step 7.4: Implement `nodes.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/nodes.py
"""Phase 1 node implementations.

Only ``ToolNode`` is wired for execution (via ToolPipeline). The other three
raise ``NotImplementedError`` until their respective phases land. The
``GraphExecutor`` catches these and logs them as warnings so the graph still
finishes gracefully.
"""
from __future__ import annotations
from typing import Any, Optional

from .graph import NodeKind
from .state import FieldRef, GraphState, Message, TypedResult


def _default_pipeline():
    """Lazy import to avoid cycles and to let tests monkeypatch."""
    from tradingagents.agent_harness.tools.pipeline import ToolPipeline
    return ToolPipeline()


class ToolNode:
    """A node that invokes a single tool via ToolPipeline (Phase 1)."""
    kind = NodeKind.TOOL

    def __init__(self, id: str, agent_id: str, tool_name: str,
                 raw_args: dict[str, Any],
                 inputs: Optional[list[FieldRef]] = None,
                 outputs: Optional[list[FieldRef]] = None) -> None:
        self.id = id
        self.agent_id = agent_id
        self.tool_name = tool_name
        self.raw_args = raw_args
        self.inputs: list[FieldRef] = list(inputs or [])
        self.outputs: list[FieldRef] = list(outputs or [])

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        # Phase 3 will resolve $refs in self.raw_args via resolver.resolve_ref.
        from tradingagents.agent_harness.tools.context import ToolContext
        ctx = ToolContext(session_id=state.run_id, intent=state.intent)
        pipeline = _default_pipeline()
        # Phase 1: avoid double-dispatch — the orchestrator's ToolRegistry
        # owns the real `tool.invoke`; here we run a no-op executor so the
        # pipeline's pre/guard hooks validate the call without dispatching.
        async def _noop_executor(args, context):
            return {"phase1_stub": True, "tool": self.tool_name, "args": args}
        pipe_res = await pipeline.run(
            tool_name=self.tool_name,
            args=dict(self.raw_args or {}),
            tool_context=ctx,
            executor=_noop_executor,
        )
        result = pipe_res.result if pipe_res.ok else {"error": pipe_res.error}
        typed = TypedResult(schema=dict, data=result, meta={"tool": self.tool_name})
        return [Message(sender=self.id, receiver="*", payload=typed)]


class LLMNode:
    """Phase 1 stub — raises NotImplementedError."""
    kind = NodeKind.LLM

    def __init__(self, id: str, agent_id: str, system_prompt: str,
                 inputs: Optional[list[FieldRef]] = None,
                 outputs: Optional[list[FieldRef]] = None) -> None:
        self.id = id
        self.agent_id = agent_id
        self.system_prompt = system_prompt
        self.inputs: list[FieldRef] = list(inputs or [])
        self.outputs: list[FieldRef] = list(outputs or [])

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        raise NotImplementedError("LLMNode lands in Phase 2")


class SubplanNode:
    """Phase 1 stub — raises NotImplementedError."""
    kind = NodeKind.SUBPLAN

    def __init__(self, id: str, agent_id: str, sub_graph,
                 inputs: Optional[list[FieldRef]] = None,
                 outputs: Optional[list[FieldRef]] = None) -> None:
        self.id = id
        self.agent_id = agent_id
        self.sub_graph = sub_graph
        self.inputs: list[FieldRef] = list(inputs or [])
        self.outputs: list[FieldRef] = list(outputs or [])

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        raise NotImplementedError("SubplanNode lands in Phase 4")


class ConsultNode:
    """Phase 1 stub — raises NotImplementedError."""
    kind = NodeKind.CONSULT

    def __init__(self, id: str, agent_id: str, target_agent: str,
                 question: str,
                 inputs: Optional[list[FieldRef]] = None,
                 outputs: Optional[list[FieldRef]] = None) -> None:
        self.id = id
        self.agent_id = agent_id
        self.target_agent = target_agent
        self.question = question
        self.inputs: list[FieldRef] = list(inputs or [])
        self.outputs: list[list[FieldRef]] = list(outputs or [])

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        raise NotImplementedError("ConsultNode lands in Phase 2")
```

- [ ] **Step 7.5: Run, expect PASS**

- [ ] **Step 7.6: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/nodes.py \
        tests/test_multi_agent_nodes.py
git commit -m "feat(runtime): §0.4.35 phase 1 — ToolNode + stubs (no _NodeBase indirection)"
```

### Task 8: `PlanCompiler` (group-order deps + correct mapping)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/compiler.py`
- Test: `tests/test_multi_agent_compiler.py`

> **`_TOOL_TO_AGENT` coupling** (rev.3 fix from review M11): committed to keeping in `core.orchestrator` for Phase 1. The docstring below makes this explicit. A future Phase 2 cleanup task can lift it if desired.

- [ ] **Step 8.1: Verify `ToolCall` shape (no `depends_on`)**

```bash
rg -n "class ToolCall" tradingagents/agent_harness/core/llm_router.py
```

- [ ] **Step 8.2: Write failing tests**

```python
# tests/test_multi_agent_compiler.py
import pytest
from tradingagents.agent_harness.runtime.multi_agent.compiler import (
    PlanCompiler, CompileError,
)
from tradingagents.agent_harness.runtime.multi_agent.graph import (
    GraphSpec, NodeKind, Edge,
)

class FakeCall:
    def __init__(self, tool, args, parallel_group=0):
        self.tool = tool
        self.args = args
        self.parallel_group = parallel_group

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


def test_compiler_two_groups_get_cross_group_data_edge_from_group_order():
    plan = FakePlan([
        FakeCall("get_quote", {"symbol": "x"}, parallel_group=0),
        FakeCall("compute_alpha", {"symbol": "x"}, parallel_group=1),
    ])
    spec = PlanCompiler().compile(plan)
    edges = {(e.src, e.dst) for e in spec.edges}
    assert ("call_0", "join_0") in edges
    assert ("call_1", "join_1") in edges
    assert ("join_0", "call_1") in edges  # cross-group from group_order


def test_compiler_three_groups_chain_through_joins():
    plan = FakePlan([
        FakeCall("get_quote", {}, parallel_group=0),
        FakeCall("get_fundamentals", {}, parallel_group=1),
        FakeCall("compute_alpha", {}, parallel_group=2),
    ])
    spec = PlanCompiler().compile(plan)
    edges = {(e.src, e.dst) for e in spec.edges}
    assert ("join_0", "call_1") in edges
    assert ("join_1", "call_2") in edges


def test_compiler_maps_tool_to_agent_via_TOOL_TO_AGENT():
    plan = FakePlan([
        FakeCall("get_quote", {}, parallel_group=0),
        FakeCall("add_to_watchlist", {}, parallel_group=0),
        FakeCall("run_trading_agents_analysis", {}, parallel_group=0),
    ])
    spec = PlanCompiler().compile(plan)
    by_id = {n.id: n for n in spec.nodes.values()}
    assert by_id["call_0"].agent_id == "data_agent"
    assert by_id["call_1"].agent_id == "command_resolver"
    assert by_id["call_2"].agent_id == "trading_agents"


def test_compiler_rejects_unknown_tool():
    plan = FakePlan([FakeCall("definitely_not_a_tool", {}, parallel_group=0)])
    with pytest.raises(CompileError):
        PlanCompiler().compile(plan)


def test_compiler_rejects_empty_plan():
    with pytest.raises(CompileError):
        PlanCompiler().compile(FakePlan([]))
```

- [ ] **Step 8.3: Run, expect FAIL**

- [ ] **Step 8.4: Implement `compiler.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/compiler.py
"""Phase 1: RouterPlan → GraphSpec compiler.

Group-order dependency inference (NOT per-call ``depends_on`` because
``ToolCall`` does not have one — see review B5). Same heuristic the live
``Orchestrator._plan_from_router()`` uses: group N depends on group N-1
when groups arrive in numerical order.

NOTE on ``_TOOL_TO_AGENT`` coupling (rev.3 fix from review M11): this
intentionally imports from ``core.orchestrator`` for Phase 1. The mapping
is internal to the orchestrator module today; lifting it to
``agents/registry.py`` would be a separate cleanup task. Not a Phase 1
concern.
"""
from __future__ import annotations
from typing import Any

from .graph import GraphSpec, Edge, BaseNode
from .nodes import ToolNode

from ..core.orchestrator import _TOOL_TO_AGENT

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
        group_order: list[int] = []

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
            g = int(getattr(call, "parallel_group", 0) or 0)
            if g not in groups:
                groups[g] = []
                group_order.append(g)
            groups[g].append(node_id)

        # Within-group edges: each call -> join_<g>
        for g in group_order:
            join_id = f"join_{g}"
            for nid in groups[g]:
                edges.append(Edge(src=nid, dst=join_id, kind="data"))

        # Cross-group edges: join_(g-1) -> first call of g
        for i in range(1, len(group_order)):
            prev_g = group_order[i - 1]
            cur_g = group_order[i]
            edges.append(Edge(
                src=f"join_{prev_g}",
                dst=groups[cur_g][0],
                kind="data",
            ))

        entry = "call_0" if calls else None
        exit_node = f"join_{group_order[-1]}" if group_order else None
        return GraphSpec(nodes=nodes, edges=edges, entry=entry, exit=exit_node)
```

- [ ] **Step 8.5: Run, expect PASS**

- [ ] **Step 8.6: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/compiler.py \
        tests/test_multi_agent_compiler.py
git commit -m "feat(runtime): §0.4.35 phase 1 — PlanCompiler (group-order cross-group deps)"
```

---

## Chunk 4: Executor + Orchestrator Wiring

### Task 9: `GraphExecutor` skeleton (inbox propagation + correct budget accounting + heartbeat logs)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/executor.py`
- Test: `tests/test_multi_agent_executor.py`

> **Critical rev.3 fix from review BLOCKER #2**: the executor MUST call `state.append_inbox(new_msg)` for every edge fire. Without this, downstream `consume_inbox()` returns empty and the data-flow architecture is hollow.
>
> **Critical rev.3 fix from review MEDIUM #8**: `state.llm_used += 1` moves to AFTER `await node.run(...)` so stubbed `LLMNode`s don't consume budget.
>
> **Critical rev.3 fix from review MEDIUM #10**: `_enqueue` takes a `source_node` argument and uses `_PRIORITY[node.kind.value]` instead of `_PRIORITY[msg.kind]` (which always defaulted to 5 because msg.kind ∈ {data,question,answer,delta}).

- [ ] **Step 9.1: Write failing tests (4 cases)**

```python
# tests/test_multi_agent_executor.py
import asyncio
from unittest.mock import MagicMock

from tradingagents.agent_harness.runtime.multi_agent.executor import GraphExecutor
from tradingagents.agent_harness.runtime.multi_agent.graph import (
    GraphSpec, Edge, NodeKind,
)
from tradingagents.agent_harness.runtime.multi_agent.nodes import (
    ToolNode, LLMNode,
)
from tradingagents.agent_harness.runtime.multi_agent.state import GraphState

async def _run(coro):
    return await coro


def _graph_two_tool_nodes():
    n1 = ToolNode(id="a", agent_id="data_agent",
                  tool_name="get_quote", raw_args={"symbol": "X"})
    n2 = ToolNode(id="b", agent_id="data_agent",
                  tool_name="get_fundamentals", raw_args={"symbol": "X"})
    return GraphSpec(
        nodes={"a": n1, "b": n2},
        edges=[Edge(src="a", dst="b", kind="data")],
        entry="a", exit="b",
    )


def test_executor_walks_linear_graph(monkeypatch):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x")
    out = _run(GraphExecutor().run(_graph_two_tool_nodes(), state))
    assert out.intent == "x"
    assert len(state.message_log) >= 2


def test_executor_populates_downstream_inbox(monkeypatch):
    """Regression for round-2 BLOCKER #2: data flow architecture must
    populate state.inbox for downstream nodes via state.append_inbox."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x")
    _run(GraphExecutor().run(_graph_two_tool_nodes(), state))

    # The cross-edge message from a -> b should be in state.message_log
    # (append_inbox appends to BOTH inbox and message_log).
    cross_messages = [
        m for m in state.message_log
        if m.receiver == "b" and m.sender == "a"
    ]
    assert len(cross_messages) >= 1, (
        f"executor did not populate inbox for downstream node b; "
        f"message_log={state.message_log}"
    )


def test_executor_budget_guard(monkeypatch):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x",
                       llm_used=5, budget_limit=5)
    state.hops_remaining = 8
    out = _run(GraphExecutor().run(_graph_two_tool_nodes(), state))
    assert out.llm_used == 5


def test_executor_swallows_not_implemented_for_llm_node(monkeypatch):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    n1 = LLMNode(id="a", agent_id="data_agent", system_prompt="...")
    spec = GraphSpec(
        nodes={"a": n1},
        edges=[],
        entry="a", exit="a",
    )
    state = GraphState(run_id="r", turn_id="t", intent="x")
    # Should NOT raise — executor logs a warning and returns
    out = _run(GraphExecutor().run(spec, state))
    assert out.intent == "x"
    # Critical rev.3 fix: stubbed LLMNode must NOT consume budget
    assert out.llm_used == 0, (
        f"stubbed LLMNode consumed llm_used={out.llm_used}; "
        f"expected 0 (rev.3 fix from MEDIUM #8)"
    )


def test_executor_emits_heartbeat_logs(monkeypatch, caplog):
    """Smoke test (Task 10.5) verifies via heartbeat logs, not absence of failure."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod
    import logging

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    caplog.set_level(logging.INFO, logger="tradingagents.agent_harness.runtime.multi_agent.executor")
    state = GraphState(run_id="r", turn_id="t", intent="x")
    _run(GraphExecutor().run(_graph_two_tool_nodes(), state))

    start_logs = [r for r in caplog.records if "GraphExecutor starting" in r.message]
    end_logs = [r for r in caplog.records if "GraphExecutor finished" in r.message]
    assert len(start_logs) >= 1, "executor did not emit start heartbeat"
    assert len(end_logs) >= 1, "executor did not emit end heartbeat"
```

- [ ] **Step 9.2: Run, expect FAIL**

- [ ] **Step 9.3: Implement `executor.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/executor.py
"""Phase 1 GraphExecutor skeleton.

Walks a GraphSpec using a priority queue. Honours budget + hop guards.
Phase 1 fully handles ``ToolNode``; the other node kinds raise
``NotImplementedError`` which the executor catches + logs as a warning so
the graph still finishes gracefully.

Rev.3 contract: every edge fire MUST call ``state.append_inbox(new_msg)``
so downstream ``consume_inbox(receiver)`` sees upstream outputs. Without
this the data-flow architecture is hollow (spec §4.7 violated).
"""
from __future__ import annotations
import heapq
import logging
from dataclasses import dataclass, field
from typing import Optional

from .graph import GraphSpec, Edge, BaseNode
from .nodes import ToolNode, LLMNode, SubplanNode, ConsultNode
from .state import GraphState, Message, TypedResult

LOGGER = logging.getLogger(__name__)

# Priority by node kind (lower = earlier)
# NodeKind values: tool=2, llm=1, subplan=4, consult=3
_PRIORITY = {
    "tool": 2,
    "llm": 1,
    "subplan": 4,
    "consult": 3,
}


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
        LOGGER.info(
            "multi_agent: GraphExecutor starting (spec=%d nodes, %d edges)",
            len(spec.nodes), len(spec.edges),
        )

        state.hops_remaining = min(state.hops_remaining, self.max_hops)
        state.budget_limit = min(state.budget_limit, self.llm_budget)

        entry = spec.entry or (next(iter(spec.nodes)) if spec.nodes else None)
        if entry is None:
            LOGGER.info("multi_agent: GraphExecutor finished (empty graph)")
            return state

        queue: list[_Pending] = []
        self._enqueue(queue, entry, Message(
            sender="__start__", receiver=entry,
            payload=TypedResult(schema=dict, data={}, meta={}),
        ), source_node=spec.node(entry))

        activated: set[str] = set()
        while queue and state.hops_remaining > 0 and state.llm_used < state.budget_limit:
            item = heapq.heappop(queue)
            msg = item.msg
            if msg.receiver not in spec.nodes:
                continue
            has_loop = any(
                e.src == msg.receiver and e.kind == "loop" for e in spec.edges
            )
            if msg.receiver in activated and not has_loop:
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
                for edge in spec.edges_from(node.id):
                    if not self._edge_fires(edge, state, m):
                        continue
                    new_msg = Message(
                        sender=node.id, receiver=edge.dst,
                        payload=m.payload, kind=m.kind, hop=m.hop + 1,
                    )
                    # CRITICAL (rev.3): append_inbox deposits into inbox AND log
                    state.append_inbox(new_msg)
                    self._enqueue(queue, edge.dst, new_msg, source_node=spec.node(edge.dst))
                    if edge.kind == "loop" and state.hops_remaining > 0:
                        state.hops_remaining -= 1
                        loop_msg = Message(
                            sender=node.id, receiver=edge.src,
                            payload=m.payload, kind=m.kind, hop=m.hop + 1,
                        )
                        state.append_inbox(loop_msg)
                        self._enqueue(queue, edge.src, loop_msg, source_node=spec.node(edge.src))
            state.hops_remaining -= 1

        LOGGER.info(
            "multi_agent: GraphExecutor finished (hops_used=%d, msgs=%d)",
            8 - state.hops_remaining, len(state.message_log),
        )
        return state

    async def _invoke(self, node, state: GraphState,
                      inbox: list[Message]) -> list[Message]:
        # NOTE: budget accounting happens AFTER the call so stubbed nodes
        # (which raise NotImplementedError caught by run()) don't consume
        # budget. See rev.3 fix from MEDIUM #8.
        if isinstance(node, ToolNode):
            out = await node.run(state, inbox)
            return out
        if isinstance(node, LLMNode):
            out = await node.run(state, inbox)
            state.llm_used += 1
            return out
        if isinstance(node, ConsultNode):
            out = await node.run(state, inbox)
            state.consultation_used += 1
            return out
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

    def _enqueue(self, queue: list[_Pending], receiver: str,
                 msg: Message, *, source_node: Optional[BaseNode] = None) -> None:
        # CRITICAL (rev.3): use source_node.kind.value, NOT msg.kind.
        # msg.kind ∈ {data, question, answer, delta} doesn't appear in _PRIORITY.
        if source_node is not None:
            prio = _PRIORITY.get(source_node.kind.value, 5)
        else:
            prio = 5
        self._seq += 1
        heapq.heappush(queue, _Pending(priority=prio, seq=self._seq, msg=msg))
```

- [ ] **Step 9.4: Run, expect PASS**

- [ ] **Step 9.5: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/executor.py \
        tests/test_multi_agent_executor.py
git commit -m "feat(runtime): §0.4.35 phase 1 — GraphExecutor (inbox propagation + budget + heartbeat)"
```

### Task 10: Wire executor into orchestrator (flag-gated, drop dead code)

**Files:**
- Modify: `tradingagents/agent_harness/runtime/multi_agent/__init__.py` (add executor/compiler/nodes to exports)
- Modify: `tradingagents/agent_harness/core/orchestrator.py`

> **Critical rev.3 fixes**:
> - `_build_graph_state` reads `orch_state.intent.value` (NOT `router_plan.intent` which doesn't exist)
> - Drops the dead `_program = _plan_from_router(router_plan, state)` call (already happens upstream at line 2247 — calling it again is duplicate work)
> - Uses `_settings.max_hops` / `_settings.llm_budget_per_turn` for `hops_remaining` / `budget_limit`

- [ ] **Step 10.1: Expand `__init__.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/__init__.py
from .graph import NodeKind, Edge, GraphSpec, BaseNode
from .state import GraphState, TypedResult, Message, FieldRef
from .settings import RuntimeSettings, load_settings
from .nodes import ToolNode, LLMNode, SubplanNode, ConsultNode
from .compiler import PlanCompiler, CompileError
from .executor import GraphExecutor

__all__ = [
    "NodeKind", "Edge", "GraphSpec", "BaseNode",
    "GraphState", "TypedResult", "Message", "FieldRef",
    "RuntimeSettings", "load_settings",
    "ToolNode", "LLMNode", "SubplanNode", "ConsultNode",
    "PlanCompiler", "CompileError",
    "GraphExecutor",
]
```

- [ ] **Step 10.2: Locate dispatch site**

```bash
rg -n "_plan_from_router|router_plan is not None" tradingagents/agent_harness/core/orchestrator.py | head
```

The branch sits inside `if router_plan is not None and router_plan.source in ("llm", "cache") and router_plan.calls:` (around line 2246).

- [ ] **Step 10.3: Add helper + flag-gated branch (no dead code)**

Add at module top of `orchestrator.py` (after existing imports):

```python
from ..runtime.multi_agent import (
    GraphExecutor, PlanCompiler, CompileError, load_settings,
)
```

Add the helper near the other `_build_*` helpers:

```python
def _build_graph_state(router_plan, orch_state, settings):
    """Build a per-turn GraphState from the orchestrator's state.

    Phase 1: shallow — only carries run_id / turn_id / intent / budget knobs.
    Phase 6 will wire AgentRuntimeStore snapshot loading.
    """
    from ..runtime.multi_agent import GraphState
    intent_obj = getattr(orch_state, "intent", None)
    intent_str = str(getattr(intent_obj, "value", "unknown")) if intent_obj else "unknown"
    return GraphState(
        run_id=getattr(orch_state, "session_id", "unknown"),
        turn_id=str(getattr(getattr(orch_state, "turn", None), "turn_id", "unknown")),
        intent=intent_str,
        hops_remaining=settings.max_hops,
        budget_limit=settings.llm_budget_per_turn,
        consultation_rate_limit=settings.consultation_rate_limit,
    )
```

At the dispatch site, immediately BEFORE the PTC dispatch (inside the `if router_plan is not None and ...` block):

```python
# §0.4.35 Phase 1 — opt-in multi-agent runtime path.
# Falls through to PTC on any exception so default behaviour is unchanged.
_settings = load_settings()
if _settings.multi_agent:
    try:
        _spec = PlanCompiler().compile(router_plan)
        _state = _build_graph_state(router_plan, state, _settings)
        await GraphExecutor(
            max_hops=_settings.max_hops,
            llm_budget=_settings.llm_budget_per_turn,
            consultation_rate_limit=_settings.consultation_rate_limit,
        ).run(_spec, _state)
        # Phase 1 keeps behaviour parity — multi_agent path is observability-only.
        # PTC still runs below for any post-graph aggregation.
    except CompileError as exc:
        LOGGER.warning(
            "graph compile failed, falling back to PTC: %s", exc,
        )
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.exception(
            "graph execution failed, falling back to PTC: %s", exc,
        )
```

- [ ] **Step 10.4: Run full test suite (default OFF)**

```bash
.venv/bin/python -m pytest tests/ -x -q
```

Expected: all pass (PTC path is the only one taken when `multi_agent=false`).

- [ ] **Step 10.5: Smoke-test the ON path (do NOT commit)**

```bash
TRADINGAGENTS_RUNTIME_MULTI_AGENT=true launchctl kickstart -k "gui/$(id -u)/com.tradingagents.web.venv"
```

Send a chat, then tail:

```bash
tail -f /tmp/tradingagents-web-venv.log | grep "multi_agent: GraphExecutor"
```

Expected output should include `multi_agent: GraphExecutor starting (spec=...)` and `multi_agent: GraphExecutor finished (...)`. Then unset:
```bash
launchctl kickstart -k "gui/$(id -u)/com.tradingagents.web.venv"
```

- [ ] **Step 10.6: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/__init__.py \
        tradingagents/agent_harness/core/orchestrator.py
git commit -m "feat(runtime): §0.4.35 phase 1 — flag-gated GraphExecutor dispatch (default off)"
```

### Task 11: End-to-end smoke test

**Files:** none (uses existing harness UI)

- [ ] **Step 11.1: Run full test suite (incl. existing runtime tests)**

```bash
.venv/bin/python -m pytest tests/test_agent_runtime_*.py tests/test_multi_agent_*.py tests/test_consult_subagent.py -q
```

Expected: all pass. Explicitly running the existing runtime tests confirms no collision with `tradingagents/agent_harness/runtime/multi_agent/`.

- [ ] **Step 11.2: Run full repo test suite**

```bash
.venv/bin/python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 11.3: Manual sanity**

- Start the service: `launchctl kickstart -k "gui/$(id -u)/com.tradingagents.web.venv"`
- Open `http://127.0.0.1:8000/`
- Send a chat; confirm `/reports` and `/scheduled` lists still render (`8f3815f` fix must not regress).
- Tail `/tmp/tradingagents-web-venv.log` for new stack traces mentioning `agent_harness.runtime.multi_agent`.

- [ ] **Step 11.4: Tag the phase**

```bash
git tag -a phase1-runtime-skeleton -m "§0.4.35 phase 1 — types + executor skeleton landed"
git push tradingagentsplus phase1-runtime-skeleton
```

---

## Acceptance Checklist (Phase 1)

- [ ] `RuntimeSettings` defaults match spec §4.9 (`multi_agent=false`, `llm_budget_per_turn=5`, `max_hops=8`, `consultation_rate_limit=0.5`)
- [ ] `TRADINGAGENTS_RUNTIME_MULTI_AGENT` env var toggles the flag (covered by `test_env_override_runtime_multi_agent`)
- [ ] `GraphState` / `TypedResult` / `Message` / `FieldRef` importable from `tradingagents.agent_harness.runtime.multi_agent`
- [ ] `GraphSpec` validates nodes + edges; `to_dict()` round-trips cleanly
- [ ] `Edge.__post_init__` rejects `kind` outside `("data", "when", "loop")`
- [ ] `resolve_ref()` handles `$agent.field`, `* N`, `? a : b`, comparison predicates, **multi-op arithmetic with correct precedence** (`$data.x * 2 + $data.y * 3` parses as `(x*2) + (y*3)`); falls back to literal on failure
- [ ] `PlanCompiler` produces a `GraphSpec` for the simple flat-parallel case AND the 2-group AND 3-group sequential cases
- [ ] `PlanCompiler` correctly maps `_TOOL_TO_AGENT` (including `command_resolver` and `trading_agents` agent-as-tool entries)
- [ ] `GraphExecutor.run()` walks a 2-node linear graph with `ToolNode` and respects budget
- [ ] `GraphExecutor` populates downstream `state.inbox` for every edge fire (regression test `test_executor_populates_downstream_inbox`)
- [ ] `GraphExecutor` swallows `NotImplementedError` for `LLMNode`/`SubplanNode`/`ConsultNode` (logs warning, continues)
- [ ] `GraphExecutor` does NOT consume `llm_used` for stubbed LLMNodes (regression test `test_executor_swallows_not_implemented_for_llm_node` asserts `out.llm_used == 0`)
- [ ] `GraphExecutor` emits `multi_agent: GraphExecutor starting/finished` heartbeat logs (regression test `test_executor_emits_heartbeat_logs`)
- [ ] `_enqueue` uses `node.kind.value` for priority (not `msg.kind`)
- [ ] `consult_subagent` stub exists; second param named `context` (not `ctx`); raises `NotImplementedError`; NOT registered yet (Phase 2)
- [ ] Default flag OFF → PTC path identical to before (all existing tests pass)
- [ ] `multi_agent=true` flag → GraphExecutor runs (heartbeat confirmed in logs, not just "no crash")
- [ ] `_build_graph_state` reads `orch_state.intent.value`, NOT `router_plan.intent` (the latter doesn't exist)
- [ ] No dead `_program = _plan_from_router(router_plan, state)` call in the wiring branch (already done upstream)
- [ ] All existing tests pass (`pytest tests/ -q`)
- [ ] `tests/test_agent_runtime_*.py` explicitly pass (collision regression guard)
- [ ] `/reports` and `/scheduled` pages still render (regression guard)
- [ ] Phase 1 tag pushed to `tradingagentsplus`

## Out of Scope (deferred to later phases)

- Phase 2: real LLM call in `LLMNode` + `ConsultNode`; consult_subagent registered via `@tool_registry.register(...)` on the per-harness `ToolRegistry`; **add `consult_subagent` to `_TOOL_TO_AGENT` mapping** (e.g. → `consult_agent` or `router`)
- Phase 3: cross-agent `$ref` resolution at runtime (in `ToolNode.run`); LLM router teaches `$ref` syntax
- Phase 4: `SubplanNode.run` + `trading_agents` graph fragment; `GraphState.fork()` lands; **`symbols` propagation into `GraphState`** (per spec §4.1)
- Phase 4: orchestration nodes (`validate_inputs`, `verify_outputs`, `aggregate_signals`) added by `PlanCompiler` per spec §4.6
- Phase 5: cutover — `multi_agent=true` becomes default
- Phase 6: mid-flight `GraphState` snapshots via `AgentRuntimeStore` (existing `tradingagents/agent_harness/runtime/store.py`)
- Phase 2 cleanup (optional): lift `_TOOL_TO_AGENT` from `core.orchestrator` into `agents/registry.py` or new `agents/tool_to_agent.py`

## Review Fixes Applied (rev. 1 → rev. 2 → rev. 3)

| ID | Rev. | Fix |
|---|---|---|
| **B1** Package collision | r1 | New sub-package `runtime/multi_agent/` — existing `runtime/` untouched |
| **B2** Design overlap | r1 | Added "Relationship to existing PlanGraph" section |
| **B3** Test dir collision | r1 | Tests moved to flat `tests/test_multi_agent_*.py` |
| **B4** No `pytest-asyncio` | r1 | All tests use `asyncio.run()` + `_run(coro)` helper |
| **B5** `ToolCall` no `depends_on` | r1 | Compiler reads cross-group deps from group order |
| **B6** Wrong config location | r1 | Settings in `default_config.py` + env override (dotted-path support) |
| **H1** ToolPipeline signature | r1 | `ToolNode.run` uses correct `await pipeline.run(...)` signature |
| **H2** Tool registration API | r1 | Phase 1 does NOT register; Phase 2 wires `@tool_registry.register(...)` |
| **H3** Edge validation | r1 | `Edge.__post_init__` validates `kind` in `("data", "when", "loop")` |
| **H4** Protocol in Pydantic field | r1 | `GraphSpec` is a `@dataclass`; custom `to_dict()` handles serialization |
| **H5** `_TOOL_TO_AGENT` coupling | r1 | Documented; Phase 3 may lift |
| **H6** Undefined helpers | r1 | `_build_graph_state` defined inline in Task 10 |
| **H7** Rollback semantics | r1 | `CompileError` AND generic `Exception` both fall through to PTC |
| **M2** Premature `fork()` | r1 | Removed; will land in Phase 4 |
| **M5** Test naming | r1 | All new tests prefixed `test_multi_agent_*` |
| **M8** `_TOOL_TO_AGENT` coverage | r1 | Test covers agent-as-tool entries |
| **L5** Chunk ordering | r1 | Compiler moved to Chunk 3 (after Nodes) |
| **BLOCKER #1** (r2) `_build_graph_state.intent` | r3 | Reads `orch_state.intent.value` (the real location); uses settings for budget knobs |
| **BLOCKER #2** (r2) Executor doesn't populate inbox | r3 | Fan-out calls `state.append_inbox(new_msg)` for every edge fire + regression test |
| **BLOCKER #3** (r2) Resolver precedence | r3 | Rightmost-split precedence at each operator level; multi-op tests added |
| **HIGH #4** (r2) Wrong file path for env overrides | r3 | All references now point to `tradingagents/default_config.py` |
| **HIGH #5** (r2) Hand-waved dotted-path | r3 | `_set_dotted` + `_lookup_dotted` helpers fully implemented + env-var test |
| **HIGH #6** (r2) `consult_subagent` ctx→context | r3 | Param renamed; signature guard test added |
| **HIGH #7** (r2) Dead `_program = _plan_from_router(...)` | r3 | Removed from wiring branch |
| **MEDIUM #8** (r2) llm_used before call | r3 | Increments AFTER `await node.run()`; regression test asserts `out.llm_used == 0` for stubbed LLM |
| **MEDIUM #9** (r2) Smoke test can't verify execution | r3 | Added `multi_agent: GraphExecutor starting/finished` heartbeat logs + test |
| **MEDIUM #10** (r2) Priority uses msg.kind | r3 | `_enqueue` takes `source_node` and uses `_PRIORITY[node.kind.value]` |
| **MEDIUM #11** (r2) `_TOOL_TO_AGENT` coupling aspirational | r3 | Docstring commits to keeping in `core.orchestrator` for Phase 1; Phase 2 cleanup tracked in Out of Scope |
| **LOW #12** (r2) `_NodeBase` parallel hierarchy | r3 | Removed; subclasses set attributes directly |
| **LOW #13** (r2) `consult_subagent` not in `_TOOL_TO_AGENT` | r3 | Added to Out of Scope as Phase 2 task |
| **LOW #14** (r2) `harness.py:82` line off-by-one | r3 | Doc now references `Harness.__init__` without line number |
| **INFO #15** (r2) `symbols`/`plan_id` not in GraphState | r3 | Added to Out of Scope as Phase 4 task |
| **INFO #16** (r2) Spec §4.6 orchestration nodes | r3 | Added to Out of Scope as Phase 4 task |
