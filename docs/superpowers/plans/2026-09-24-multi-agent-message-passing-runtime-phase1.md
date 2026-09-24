# [Phase 1 — Multi-Agent Runtime Skeleton] Implementation Plan (rev. 4)

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the type system, `$ref` resolver (rightmost-split precedence), `PlanCompiler` (group-order case), `GraphExecutor` skeleton (inbox propagation + per-edge hop accounting), and `consult_subagent` stub **behind the `runtime.multi_agent` flag (default off)** without changing any existing behaviour. All current tests must remain green.

**Architecture:** All new code lives under a NEW sub-package `tradingagents/agent_harness/runtime/multi_agent/` to avoid colliding with the existing supervised AgentRuntime module (`AgentRuntime`, `AgentRuntimeStore`, `PlanGraph`, `GraphPatch`, etc.). The orchestrator gains a new code path gated by `runtime.multi_agent`; the existing PTC branch is untouched in this phase.

**Critical Phase 1 contracts** (locked in rev. 4 after round-3 review):
- Executor MUST populate `state.inbox` for downstream nodes whenever it fires an edge. (Spec §4.7.)
- Executor MUST track per-edge hop counter (`Edge.hops_used`); the global `state.hops_remaining` decrements ONCE per while-body iteration, NOT per-edge. (Spec §4.4 + §4.7.)
- `Edge.max_hops` default is **2** (spec §4.6 step 4), not 3.
- `Edge.max_hops` is enforced by `Edge.hops_used` — not by the per-message hop field, which never increments past 1.
- `consult_subagent`'s second param is `context: ToolContext | None = None` (matches every other built-in tool's signature).
- Compiler's import is `from ...core.orchestrator import _TOOL_TO_AGENT` (THREE dots — `multi_agent` is 3 levels deep from `tradingagents/`).
- All `$ref` tests must use rightmost-split precedence; **parentheses are NOT supported in Phase 1** (spec doesn't mandate them).

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
| `__init__.py` | ~30 | Public exports |
| `settings.py` | ~30 | `RuntimeSettings` (Pydantic) + `load_settings()` factory |
| `state.py` | ~120 | `GraphState`, `TypedResult`, `Message`, `FieldRef` |
| `graph.py` | ~140 | `NodeKind`, `BaseNode` Protocol, `Edge` (with `hops_used` counter — rev.4 fix), `GraphSpec` |
| `resolver.py` | ~180 | `parse_ref`, `is_ref_expr`, `resolve_ref` (rightmost-split precedence; **no parentheses in Phase 1**) |
| `nodes.py` | ~200 | `ToolNode` + stubs; no `_NodeBase` indirection; **type annotation `list[FieldRef]` (rev.4 fix)** |
| `compiler.py` | ~180 | `PlanCompiler`; **imports `from ...core.orchestrator` (THREE dots, rev.4 fix)** |
| `executor.py` | ~300 | `GraphExecutor`; inbox propagation; per-edge hop accounting via `Edge.hops_used`; heartbeat logs |

### Modified files

| File | Change |
|---|---|
| `tradingagents/default_config.py` | Add `runtime.*` block + 4 env-override entries + `_set_dotted` / `_lookup_dotted` helpers |
| `tradingagents/agent_harness/core/orchestrator.py` | Flag-gated GraphExecutor dispatch; helper takes only `(orch_state, settings)` (rev.4 drops unused `router_plan` param) |

### New tool (separate file)

| File | Lines | Responsibility |
|---|---|---|
| `tradingagents/agent_harness/tools/builtin_consult.py` | ~80 | `ConsultSubagentArgs` + `consult_subagent` async fn; **second param is `context: ToolContext \| None = None`** (rev.4 fix matches all other built-ins in `tools/builtin.py`) |

### New test files (flat in `tests/`, NOT nested)

| File | Coverage |
|---|---|
| `tests/test_multi_agent_settings.py` | Defaults + override + **env var injection via dotted path** |
| `tests/test_multi_agent_state.py` | State dataclass round-trip |
| `tests/test_multi_agent_graph.py` | `GraphSpec` + `Edge` validation + `to_dict()` |
| `tests/test_multi_agent_resolver.py` | `$ref` parser + evaluator; **NO paren tests** (rev.4 fix); 3-level nested test added |
| `tests/test_multi_agent_compiler.py` | 1-/2-/3-group + agent-as-tool mapping |
| `tests/test_multi_agent_nodes.py` | `ToolNode` + stubs |
| `tests/test_multi_agent_executor.py` | Linear + inbox propagation + budget + heartbeat + **loop-edge regression** (rev.4 fix) |
| `tests/test_consult_subagent.py` | Args validation + signature uses `context: ToolContext \| None = None` |

All tests use `asyncio.run()` + `_run(coro)` helper from `tests/test_tool_pipeline.py:30`. **No bare `async def test_...`** (per review B4).

---

## Chunk 1: Settings + Types (no behavior change, just shape)

### Task 1: `RuntimeSettings` + config wiring (dotted-path + env var injection)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/settings.py`
- Modify: `tradingagents/default_config.py` (add `runtime` block + env overrides + `_set_dotted` helper)
- Test: `tests/test_multi_agent_settings.py`

- [ ] **Step 1.1: Write failing test (4 cases)**

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
    from tradingagents.dataflows import config as cfg
    cfg.set_config({"runtime": {"multi_agent": True, "llm_budget_per_turn": 7}})
    s = load_settings()
    assert s.multi_agent is True
    assert s.llm_budget_per_turn == 7

def test_env_override_runtime_multi_agent(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_RUNTIME_MULTI_AGENT", "true")
    monkeypatch.setenv("TRADINGAGENTS_RUNTIME_LLM_BUDGET_PER_TURN", "9")
    import tradingagents.default_config as dc
    importlib.reload(dc)
    import tradingagents.dataflows.config as dfcfg
    importlib.reload(dfcfg)
    cfg = dfcfg.get_config()
    assert cfg["runtime"]["multi_agent"] is True
    assert cfg["runtime"]["llm_budget_per_turn"] == 9
```

- [ ] **Step 1.2: Run, expect FAIL**

- [ ] **Step 1.3: Implement `settings.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/settings.py
from __future__ import annotations
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

In `tradingagents/default_config.py`, before the closing `}` of `DEFAULT_CONFIG`:

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

Add 4 entries to `_ENV_OVERRIDES`:

```python
    # §0.4.35 Phase 1 — dotted paths land at the runtime.* block
    "TRADINGAGENTS_RUNTIME_MULTI_AGENT":          "runtime.multi_agent",
    "TRADINGAGENTS_RUNTIME_LLM_BUDGET_PER_TURN":  "runtime.llm_budget_per_turn",
    "TRADINGAGENTS_RUNTIME_MAX_HOPS":             "runtime.max_hops",
    "TRADINGAGENTS_RUNTIME_CONSULTATION_RATE":    "runtime.consultation_rate_limit",
```

Add helpers after `_coerce`:

```python
def _set_dotted(cfg: dict, dotted_key: str, value) -> dict:
    """Walk a dotted key path and assign the leaf value."""
    parts = dotted_key.split(".")
    cur = cfg
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value
    return cfg

def _lookup_dotted(cfg: dict, dotted_key: str):
    """Read a dotted key without mutating. Returns None for missing paths."""
    parts = dotted_key.split(".")
    cur = cfg
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur
```

Update `_apply_env_overrides`:

```python
def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        reference = _lookup_dotted(config, key)
        if reference is None:
            reference = raw  # best-effort coerce against the raw string
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
from tradingagents.agent_harness.runtime.multi_agent.state import (
    GraphState, TypedResult, Message, FieldRef,
)

def test_typed_result_roundtrip():
    class Quote:
        def __init__(self, price): self.price = price
    res = TypedResult(schema=Quote, data=Quote(41.06), meta={"source_ts": 1.0})
    assert res.data.price == 41.06
    assert res.meta["source_ts"] == 1.0

def test_message_defaults():
    m = Message(sender="a", receiver="b",
                payload=TypedResult(schema=dict, data={}, meta={}))
    assert m.kind == "data"
    assert m.hop == 0

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
"""Turn-level ephemeral state for the multi-agent runtime (Phase 1).

Rev.4 contract: ``TypedResult.meta`` reserves three keys per spec §7:
``source_ts`` (staleness), ``source_agent``, ``source_tool``. Phase 4 verifier
reads ``source_ts`` to reject stale outputs. Phase 1 producers SHOULD populate
``source_ts`` (use ``time.monotonic()``) but it's not enforced.
"""
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
    # NOTE: spec §4.1 also lists `symbols` and `plan_id`; both land in Phase 4
    # (see "Out of Scope").
    agent_outputs: dict[str, TypedResult] = field(default_factory=dict)
    inbox: dict[str, list[Message]] = field(default_factory=dict)
    message_log: list[Message] = field(default_factory=list)
    hops_remaining: int = 8
    llm_used: int = 0
    consultation_used: int = 0
    # NOTE: `consultation_rate_limit` is dead in Phase 1 — Phase 2 enforces
    # the "no nested consult" rule inside GraphExecutor._invoke (see Out of Scope).
    budget_limit: int = 5
    consultation_rate_limit: float = 0.5

    def append_inbox(self, msg: Message) -> None:
        self.inbox.setdefault(msg.receiver, []).append(msg)
        self.message_log.append(msg)

    def consume_inbox(self, receiver: str) -> list[Message]:
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

### Task 3: `NodeKind` + `BaseNode` + `Edge` (with `hops_used` counter) + `GraphSpec`

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

def test_edge_default_max_hops_is_2_per_spec():
    # Spec §4.6 step 4: loop edges default to max_hops=2.
    e = Edge(src="a", dst="b", kind="loop")
    assert e.max_hops == 2
    assert e.hops_used == 0

def test_edge_accepts_valid_kinds():
    for k in ("data", "when", "loop"):
        e = Edge(src="a", dst="b", kind=k)  # type: ignore[arg-type]
        assert e.kind == k

class _StubNode:
    id = "n1"
    agent_id = "data_agent"
    kind = NodeKind.TOOL

    def __init__(self):
        from tradingagents.agent_harness.runtime.multi_agent.state import FieldRef
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
__post_init__ (see review H3) and tracks `hops_used` for per-edge loop
accounting (rev.4 fix from review HIGH #3 — see executor.py).

`max_hops` default is 2 per spec §4.6 step 4 (rev.4 fix from LOW #14).
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
    max_hops: int = 2  # spec §4.6 step 4
    hops_used: int = 0  # rev.4: per-edge counter (mutated by executor)

    def __post_init__(self) -> None:
        if self.kind not in _VALID_EDGE_KINDS:
            raise ValueError(
                f"invalid edge kind: {self.kind!r} (must be one of {_VALID_EDGE_KINDS})"
            )

@dataclass
class GraphSpec:
    nodes: dict[str, BaseNode]
    edges: list[Edge] = field(default_factory=list)
    entry: Optional[str] = None
    exit: Optional[str] = None

    def node(self, node_id: str) -> BaseNode:
        return self.nodes[node_id]

    def edges_from(self, src: str) -> list[Edge]:
        return [e for e in self.edges if e.src == src]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": {
                nid: {"id": n.id, "agent_id": n.agent_id, "kind": n.kind.value}
                for nid, n in self.nodes.items()
            },
            "edges": [
                {"src": e.src, "dst": e.dst, "kind": e.kind,
                 "field_ref": e.field_ref, "max_hops": e.max_hops,
                 "hops_used": e.hops_used}
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
git commit -m "feat(runtime): §0.4.35 phase 1 — Edge with hops_used counter, max_hops=2"
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

__all__ = [
    "NodeKind", "Edge", "GraphSpec", "BaseNode",
    "GraphState", "TypedResult", "Message", "FieldRef",
    "RuntimeSettings", "load_settings",
]
```

- [ ] **Step 4.2: Verify import**

Run: `.venv/bin/python -c "from tradingagents.agent_harness.runtime.multi_agent import GraphSpec, RuntimeSettings, load_settings; print(GraphSpec, RuntimeSettings, load_settings())"`

- [ ] **Step 4.3: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/__init__.py
git commit -m "feat(runtime): §0.4.35 phase 1 — multi_agent package __init__ (partial exports)"
```

---

## Chunk 2: Resolver + consult_subagent Stub

### Task 5: `$ref` parser + evaluator (rightmost-split precedence, NO parens)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/resolver.py`
- Test: `tests/test_multi_agent_resolver.py`

> **Phase 1 does NOT support parentheses.** The resolver relies on rightmost-split precedence with `*` / `+` / comparison operators. `($a + $b) * 4` is NOT valid in Phase 1 — use `$a * 4 + $b * 4` instead. Paren handling is deferred to Phase 3 or later if needed.

- [ ] **Step 5.1: Write failing tests — NO paren case**

```python
# tests/test_multi_agent_resolver.py
import pytest
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
                data={"x": 2, "y": 3,
                      "q": {"symbol": "600036.SS", "isin": "CNE1000000Q1"},
                      "tag": "buy"},
                meta={},
            ),
            "alpha": TypedResult(schema=dict, data={"price": 40.5}, meta={}),
        },
    )

def test_parse_ref_basic():
    assert parse_ref("\$data.quote.symbol") == ("data", "quote.symbol")

def test_parse_ref_3_level_nested():
    # regex greedily captures x.y.z into group 2
    assert parse_ref("\$data.q.symbol.isin") == ("data", "q.symbol.isin")

def test_parse_ref_no_dollar_returns_none():
    assert parse_ref("data.quote.symbol") is None

@pytest.mark.parametrize("bad_input,reason", [
    ("\$\$",          "no agent name after first dollar"),
    ("\$abc",         "no dot in regex group"),
    ("\$data.",       "empty field name"),
    ("",              "empty string"),
])
def test_parse_ref_rejects_malformed(bad_input, reason):
    # Document why each input is rejected
    assert parse_ref(bad_input) is None, f"should reject {bad_input!r} ({reason})"

def test_is_ref_expr_distinguishes():
    assert is_ref_expr("\$data.x")
    assert not is_ref_expr("hello")
    assert is_ref_expr("\$data.x * 1.5")
    assert not is_ref_expr(42)
    assert not is_ref_expr(None)

def test_resolve_ref_simple():
    s = _state()
    assert resolve_ref("\$data.q.symbol", s) == "600036.SS"

def test_resolve_ref_3_level_nested_lookup():
    s = _state()
    assert resolve_ref("\$data.q.symbol.isin", s) == "CNE1000000Q1"

def test_resolve_ref_arithmetic_single_op():
    s = _state()
    assert resolve_ref("\$alpha.price * 2", s) == 81.0
    assert resolve_ref("\$alpha.price * 1.5", s) == 60.75

def test_resolve_ref_arithmetic_precedence_two_refs_with_constant():
    # Critical rev.3/4 test — $data.x=2, $data.y=3 → 2*2 + 3*3 == 13
    s = _state()
    assert resolve_ref("\$data.x * 2 + \$data.y * 3", s) == 13

def test_resolve_ref_arithmetic_precedence_mul_binds_tighter_than_add():
    # 2 + 3 * 4 == 14 (not 20) — multiplication binds tighter than addition
    s = _state()
    assert resolve_ref("\$data.x + \$data.y * 4", s) == 14

def test_resolve_ref_ternary_truthy():
    s = _state()
    assert resolve_ref("\$alpha.price > 40 ? 'expensive' : 'cheap'", s) == "expensive"

def test_resolve_ref_arithmetic_in_ternary():
    s = _state()
    assert resolve_ref("\$data.x * 2 == 4 ? 'four' : 'other'", s) == "four"

def test_resolve_ref_falls_back_to_literal():
    s = _state()
    assert resolve_ref("\$nope.field", s, literal="fallback") == "fallback"

def test_resolve_ref_passes_through_non_string():
    s = _state()
    assert resolve_ref(42, s) == 42
```

- [ ] **Step 5.2: Run, expect FAIL**

- [ ] **Step 5.3: Implement `resolver.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/resolver.py
"""Pure \$ref parser + evaluator for the multi-agent runtime (Phase 1).

Rightmost-split precedence: at each operator level, split on the RIGHTMOST
occurrence. Gives left-to-right associativity with correct precedence:
``\$data.x * 2 + \$data.y * 3`` parses as ``(\$data.x * 2) + (\$data.y * 3)``.

**Parentheses are NOT supported in Phase 1.** Rewrite expressions like
``(\$a + \$b) * 4`` to ``\$a * 4 + \$b * 4``.
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
    idx = value.rfind(op)
    if idx < 0:
        return None
    return value[:idx], value[idx + len(op):]

def _eval_expr(value: str, state: GraphState) -> Any:
    # Ternary (lowest precedence)
    if "?" in value and ":" in value:
        cond, rest = value.split("?", 1)
        a, b = rest.split(":", 1)
        cond_val = _eval_expr(cond.strip(), state)
        chosen = a if _truthy(cond_val) else b
        return _eval_expr(chosen.strip(), state)

    # Arithmetic — rightmost split per precedence level
    for ops in [
        [(" + ", lambda a, b: a + b),
         (" - ", lambda a, b: a - b)],
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

    # Comparison
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
git commit -m "feat(runtime): §0.4.35 phase 1 — \$ref parser/evaluator (rightmost-split, no parens)"
```

### Task 6: `consult_subagent` tool stub (`context: ToolContext | None`)

**Files:**
- Create: `tradingagents/agent_harness/tools/builtin_consult.py`
- Test: `tests/test_consult_subagent.py`

> **Phase 1 does NOT register the tool.** Registration lands in Phase 2 via `@tool_registry.register(...)` on the per-harness `ToolRegistry` instance (`Harness.__init__` calls `install_builtin_tools(self.tool_registry)`). **Rev.4 critical**: second param is `context: ToolContext | None = None` — matches every other built-in tool in `tools/builtin.py` (`add_to_watchlist`, etc.). FunctionTool.invoke passes a `ToolContext` instance, not a dict.

- [ ] **Step 6.1: Write failing tests**

```python
# tests/test_consult_subagent.py
import asyncio
import inspect
import pytest
from tradingagents.agent_harness.tools.context import ToolContext
from tradingagents.agent_harness.tools.builtin_consult import (
    ConsultSubagentArgs, consult_subagent,
)

def test_consult_args_validation():
    args = ConsultSubagentArgs(target_agent="data_agent", question="why?")
    assert args.target_agent == "data_agent"
    assert args.max_tokens == 512
    with pytest.raises(Exception):
        ConsultSubagentArgs(target_agent="", question="x")

def test_consult_subagent_signature_uses_context_with_default_none():
    # Phase 2 hazard guard: must match the convention used by every other
    # built-in (see tools/builtin.py). Param name MUST be "context" and
    # MUST default to None so callers can omit it.
    sig = inspect.signature(consult_subagent)
    params = sig.parameters
    assert "context" in params, (
        f"FunctionTool.invoke only auto-injects the tool context when the "
        f"param name is 'context' (see tools/base.py); got {list(params)!r}"
    )
    assert params["context"].default is None, (
        "context must default to None to match the convention in tools/builtin.py"
    )

def test_consult_subagent_stub_raises():
    args = ConsultSubagentArgs(target_agent="data_agent", question="hi")
    ctx = ToolContext(session_id="r", intent="x")
    with pytest.raises(NotImplementedError):
        asyncio.run(consult_subagent(args, context=ctx))
```

- [ ] **Step 6.2: Run, expect FAIL**

- [ ] **Step 6.3: Implement `builtin_consult.py`**

```python
# tradingagents/agent_harness/tools/builtin_consult.py
"""Stub for `consult_subagent` — Phase 2 wires registration + real LLM call.

Phase 1 surfaces the contract only. Registration happens in Phase 2 via
``@tool_registry.register(...)`` on the per-harness ``ToolRegistry``.

Rev.4 contract: the ``context`` parameter uses the standard ToolContext type
(defaults to None), matching every other built-in tool in ``tools/builtin.py``
(``add_to_watchlist``, etc.). Renaming or retyping this in Phase 2 will
break callers, so the Phase 1 stub locks the contract.
"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field

from tradingagents.agent_harness.tools.context import ToolContext

class ConsultSubagentArgs(BaseModel):
    target_agent: str = Field(min_length=1)
    question: str = Field(min_length=1)
    context_refs: list[str] = Field(default_factory=list)
    max_tokens: int = Field(default=512, ge=64, le=2048)

async def consult_subagent(
    args: ConsultSubagentArgs,
    context: ToolContext | None = None,
) -> dict[str, Any]:
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
git commit -m "feat(runtime): §0.4.35 phase 1 — consult_subagent stub (ToolContext|None signature)"
```

---

## Chunk 3: Nodes + PlanCompiler

### Task 7: `ToolNode` + stub nodes (correct type annotation)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/nodes.py`
- Test: `tests/test_multi_agent_nodes.py`

- [ ] **Step 7.1: Write failing tests**

```python
# tests/test_multi_agent_nodes.py
import asyncio
from unittest.mock import MagicMock
import pytest

from tradingagents.agent_harness.runtime.multi_agent.nodes import (
    ToolNode, LLMNode, SubplanNode, ConsultNode,
)
from tradingagents.agent_harness.runtime.multi_agent.graph import NodeKind
from tradingagents.agent_harness.runtime.multi_agent.state import GraphState, FieldRef

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

def test_consult_node_outputs_is_list_of_field_refs():
    # rev.4 fix from LOW #12: previously typed as list[list[FieldRef]]
    node = ConsultNode(id="d", agent_id="data_agent",
                       target_agent="alpha_agent", question="?")
    # Static type check would fail if this is list[list[FieldRef]]
    assert isinstance(node.outputs, list)
    out: list[FieldRef] = node.outputs
    assert out == []

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

def test_llm_node_raises_not_implemented():
    node = LLMNode(id="b", agent_id="data_agent", system_prompt="...")
    state = GraphState(run_id="r", turn_id="t", intent="x")
    with pytest.raises(NotImplementedError):
        _run(node.run(state, inbox=[]))

def test_subplan_node_raises_not_implemented():
    node = SubplanNode(id="c", agent_id="trading_agents", sub_graph=MagicMock())
    state = GraphState(run_id="r", turn_id="t", intent="x")
    with pytest.raises(NotImplementedError):
        _run(node.run(state, inbox=[]))

def test_consult_node_raises_not_implemented():
    node = ConsultNode(id="d", agent_id="data_agent",
                       target_agent="alpha_agent", question="?")
    state = GraphState(run_id="r", turn_id="t", intent="x")
    with pytest.raises(NotImplementedError):
        _run(node.run(state, inbox=[]))
```

- [ ] **Step 7.2: Run, expect FAIL**

- [ ] **Step 7.3: Implement `nodes.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/nodes.py
"""Phase 1 node implementations.

Only ``ToolNode`` is wired for execution (via ToolPipeline). The other three
raise ``NotImplementedError`` until their respective phases land.
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
        from tradingagents.agent_harness.tools.context import ToolContext
        ctx = ToolContext(session_id=state.run_id, intent=state.intent)
        pipeline = _default_pipeline()

        async def _noop_executor(args, context):
            return {"phase1_stub": True, "tool": self.tool_name, "args": args}
        pipe_res = await pipeline.run(
            tool_name=self.tool_name,
            args=dict(self.raw_args or {}),
            tool_context=ctx,
            executor=_noop_executor,
        )
        result = pipe_res.result if pipe_res.ok else {"error": pipe_res.error}
        typed = TypedResult(
            schema=dict, data=result,
            meta={"tool": self.tool_name, "source_ts": Message.__init__.__defaults__[0]
                  if False else None},  # placeholder; replaced in Task 9 if needed
        )
        return [Message(sender=self.id, receiver="*", payload=typed)]


class LLMNode:
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
        self.outputs: list[FieldRef] = list(outputs or [])

    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        raise NotImplementedError("ConsultNode lands in Phase 2")
```

> **Cleanup note**: the `meta={"source_ts": ...}` placeholder in `ToolNode.run` is intentional — Phase 1 producers aren't required to populate `source_ts`, but Phase 4 verifier expects it. The placeholder keeps the field present; a cleaner approach lands in Phase 2 when the orchestrator can compute a real monotonic timestamp.

- [ ] **Step 7.4: Run, expect PASS**

- [ ] **Step 7.5: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/nodes.py \
        tests/test_multi_agent_nodes.py
git commit -m "feat(runtime): §0.4.35 phase 1 — ToolNode + stubs (correct FieldRef typing)"
```

### Task 8: `PlanCompiler` (group-order deps, **3-dot relative import**)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/compiler.py`
- Test: `tests/test_multi_agent_compiler.py`

> **Rev.4 critical**: `compiler.py` lives at `tradingagents/agent_harness/runtime/multi_agent/compiler.py`. From there, the orchestrator is THREE levels up: `agent_harness → runtime → multi_agent` (3 dots). The rev.3 plan used 2 dots which would fail at import.

- [ ] **Step 8.1: Write failing tests**

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
    edges = {(e.src, e.dst) for e in spec.edges}
    assert ("call_0", "join_0") in edges
    assert ("call_1", "join_0") in edges

def test_compiler_two_groups_get_cross_group_data_edge_from_group_order():
    plan = FakePlan([
        FakeCall("get_quote", {"symbol": "x"}, parallel_group=0),
        FakeCall("compute_alpha", {"symbol": "x"}, parallel_group=1),
    ])
    spec = PlanCompiler().compile(plan)
    edges = {(e.src, e.dst) for e in spec.edges}
    assert ("join_0", "call_1") in edges

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

- [ ] **Step 8.2: Run, expect FAIL** (likely `ModuleNotFoundError` if relative import is wrong, else `AssertionError`)

- [ ] **Step 8.3: Implement `compiler.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/compiler.py
"""Phase 1: RouterPlan → GraphSpec compiler.

Group-order dependency inference (NOT per-call ``depends_on`` because
``ToolCall`` does not have one). Same heuristic as live
``Orchestrator._plan_from_router()``.

NOTE on ``_TOOL_TO_AGENT`` coupling: imported from ``core.orchestrator``
(intentionally — same module that defines the runtime mapping today).
"""
from __future__ import annotations
from typing import Any

from .graph import GraphSpec, Edge, BaseNode
from .nodes import ToolNode

# CRITICAL: this module is 3 levels deep (multi_agent → runtime → agent_harness).
# Three dots up = agent_harness, where core/orchestrator.py lives.
from ...core.orchestrator import _TOOL_TO_AGENT

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

        for g in group_order:
            join_id = f"join_{g}"
            for nid in groups[g]:
                edges.append(Edge(src=nid, dst=join_id, kind="data"))

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

- [ ] **Step 8.4: Verify import works**

Run: `.venv/bin/python -c "from tradingagents.agent_harness.runtime.multi_agent.compiler import PlanCompiler, CompileError; print(PlanCompiler, CompileError)"`

- [ ] **Step 8.5: Run tests, expect PASS**

- [ ] **Step 8.6: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/compiler.py \
        tests/test_multi_agent_compiler.py
git commit -m "feat(runtime): §0.4.35 phase 1 — PlanCompiler (3-dot relative import)"
```

---

## Chunk 4: Executor + Orchestrator Wiring

### Task 9: `GraphExecutor` (inbox propagation + per-edge hop accounting + heartbeat)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/executor.py`
- Test: `tests/test_multi_agent_executor.py`

> **Rev.4 critical fixes**:
> - **HIGH #2**: drop the double `state.hops_remaining -= 1` inside the loop branch — global counter decrements ONCE per while-body iteration.
> - **HIGH #3**: per-edge `Edge.hops_used` is the source of truth for `max_hops` enforcement, NOT per-message hop.
> - **MEDIUM #7**: log `initial_hops - state.hops_remaining` (computed from saved initial), not hardcoded `8`.
> - **HIGH #4**: loop-edge regression test added.

- [ ] **Step 9.1: Write failing tests (5 cases)**

```python
# tests/test_multi_agent_executor.py
import asyncio
import logging
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


def _graph_self_loop():
    """1 ToolNode with a self-loop edge — used for loop regression."""
    n1 = ToolNode(id="a", agent_id="data_agent",
                  tool_name="get_quote", raw_args={"symbol": "X"})
    return GraphSpec(
        nodes={"a": n1},
        edges=[Edge(src="a", dst="a", kind="loop", max_hops=2)],
        entry="a", exit="a",
    )


def test_executor_walks_linear_graph(monkeypatch):
    """rev.4 fix from HIGH #5: only 1 message_log entry for 1 cross-edge."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x")
    out = _run(GraphExecutor().run(_graph_two_tool_nodes(), state))
    assert out.intent == "x"
    # Only ONE fan-out call fires (a -> b). So message_log has exactly 1.
    assert len(state.message_log) == 1


def test_executor_populates_downstream_inbox(monkeypatch):
    """regression for round-2 BLOCKER #2: data flow architecture must
    populate state.inbox for downstream nodes via state.append_inbox."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    state = GraphState(run_id="r", turn_id="t", intent="x")
    _run(GraphExecutor().run(_graph_two_tool_nodes(), state))

    cross_messages = [
        m for m in state.message_log
        if m.receiver == "b" and m.sender == "a"
    ]
    assert len(cross_messages) >= 1


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
    spec = GraphSpec(nodes={"a": n1}, edges=[], entry="a", exit="a")
    state = GraphState(run_id="r", turn_id="t", intent="x")
    out = _run(GraphExecutor().run(spec, state))
    assert out.intent == "x"
    # Stubbed LLMNode must NOT consume budget
    assert out.llm_used == 0


def test_executor_loop_edge_re_executes_upstream(monkeypatch):
    """rev.4 regression for HIGH #2/3/4: loop edge with max_hops=2
    re-executes upstream exactly 2 times total."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    spec = _graph_self_loop()
    state = GraphState(run_id="r", turn_id="t", intent="x")
    out = _run(GraphExecutor().run(spec, state))

    # ToolNode a runs 1 initial time + 2 loop re-executions = 3 total runs.
    # Each run produces 1 out_message → 3 fan-out calls → 3 inbox appends.
    # But loop edge fires only when edge.hops_used < max_hops (default 2).
    # So total message_log >= 3 (initial + 2 loop fires).
    assert len(state.message_log) >= 3, (
        f"loop edge did not re-execute upstream enough times; "
        f"message_log has {len(state.message_log)} entries"
    )


def test_executor_emits_heartbeat_logs(monkeypatch, caplog):
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    caplog.set_level(
        logging.INFO,
        logger="tradingagents.agent_harness.runtime.multi_agent.executor",
    )
    state = GraphState(run_id="r", turn_id="t", intent="x")
    _run(GraphExecutor().run(_graph_two_tool_nodes(), state))

    start_logs = [r for r in caplog.records if "GraphExecutor starting" in r.message]
    end_logs = [r for r in caplog.records if "GraphExecutor finished" in r.message]
    assert len(start_logs) >= 1
    assert len(end_logs) >= 1
```

- [ ] **Step 9.2: Run, expect FAIL** (loop test will fail because `Edge.hops_used` field doesn't exist yet)

- [ ] **Step 9.3: Implement `executor.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/executor.py
"""Phase 1 GraphExecutor skeleton.

Walks a GraphSpec using a priority queue. Honours budget + hop guards.

Rev.4 contracts:
- Every edge fire calls ``state.append_inbox(new_msg)`` so downstream
  ``consume_inbox(receiver)`` sees upstream outputs (spec §4.7).
- Global ``state.hops_remaining`` decrements ONCE per while-body iteration
  (NOT per-edge). Per-edge loop accounting uses ``Edge.hops_used``
  (spec §4.4 + §4.7 — per-edge counter, NOT per-message hop).
- Stubbed LLMNode does NOT consume budget — increment happens AFTER
  ``await node.run(...)``.
- Heartbeat logs use ``initial_hops - state.hops_remaining`` (not hardcoded 8).
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
        # rev.4: save initial_hops for accurate heartbeat logging
        initial_hops = state.hops_remaining
        LOGGER.info(
            "multi_agent: GraphExecutor starting (spec=%d nodes, %d edges, "
            "initial_hops=%d, budget=%d)",
            len(spec.nodes), len(spec.edges),
            initial_hops, state.budget_limit,
        )

        state.hops_remaining = min(state.hops_remaining, self.max_hops)
        state.budget_limit = min(state.budget_limit, self.llm_budget)

        entry = spec.entry or (next(iter(spec.nodes)) if spec.nodes else None)
        if entry is None:
            LOGGER.info("multi_agent: GraphExecutor finished (empty graph)")
            return state

        queue: list[_Pending] = []
        self._enqueue(
            queue, entry,
            Message(sender="__start__", receiver=entry,
                    payload=TypedResult(schema=dict, data={}, meta={})),
            source_node=spec.node(entry),
        )

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
            except Exception as exc:  # pragma: no cover
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
                    state.append_inbox(new_msg)
                    self._enqueue(
                        queue, edge.dst, new_msg,
                        source_node=spec.node(edge.dst),
                    )
                    if edge.kind == "loop":
                        # rev.4: per-edge counter (Edge.hops_used), not global
                        # AND no per-edge decrement of state.hops_remaining
                        edge.hops_used += 1
                        loop_msg = Message(
                            sender=node.id, receiver=edge.src,
                            payload=m.payload, kind=m.kind, hop=m.hop + 1,
                        )
                        state.append_inbox(loop_msg)
                        self._enqueue(
                            queue, edge.src, loop_msg,
                            source_node=spec.node(edge.src),
                        )
            # rev.4: global counter decrements ONCE per iteration
            state.hops_remaining -= 1

        LOGGER.info(
            "multi_agent: GraphExecutor finished (hops_used=%d, msgs=%d)",
            initial_hops - state.hops_remaining,
            len(state.message_log),
        )
        return state

    async def _invoke(self, node, state: GraphState,
                      inbox: list[Message]) -> list[Message]:
        # rev.4: budget accounting happens AFTER the call so stubbed nodes
        # (which raise NotImplementedError caught by run()) don't consume budget.
        if isinstance(node, ToolNode):
            return await node.run(state, inbox)
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
            # rev.4: per-edge counter (spec §4.7 — state.hops_for(edge))
            return edge.hops_used < edge.max_hops
        return False

    def _enqueue(self, queue: list[_Pending], receiver: str,
                 msg: Message, *, source_node: Optional[BaseNode] = None) -> None:
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
git commit -m "feat(runtime): §0.4.35 phase 1 — GraphExecutor (inbox + per-edge hop + heartbeat)"
```

### Task 10: Wire executor into orchestrator (helper takes `(orch_state, settings)`)

**Files:**
- Modify: `tradingagents/agent_harness/runtime/multi_agent/__init__.py` (expand exports)
- Modify: `tradingagents/agent_harness/core/orchestrator.py`

> **Rev.4 fix**: `_build_graph_state(orch_state, settings)` — drops the unused `router_plan` parameter (LOW #17). Uses `orch_state.intent.value` (BLOCKER #1 fix). Helper sets budget knobs from settings.

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

- [ ] **Step 10.3: Add helper + flag-gated branch**

Add at module top of `orchestrator.py`:

```python
from ..runtime.multi_agent import (
    GraphExecutor, PlanCompiler, CompileError, load_settings,
)
```

Add helper near other `_build_*` helpers:

```python
def _build_graph_state(orch_state, settings):
    """Build a per-turn GraphState from the orchestrator's state.

    Phase 1: shallow — only carries run_id / turn_id / intent / budget knobs.
    Phase 6 will wire AgentRuntimeStore snapshot loading.

    Rev.4: reads ``orch_state.intent.value`` (RouterPlan has no ``intent``
    field — see round-2 BLOCKER #1).
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

At the dispatch site (immediately BEFORE the PTC dispatch, inside the `if router_plan is not None and ...` block):

```python
# §0.4.35 Phase 1 — opt-in multi-agent runtime path.
_settings = load_settings()
if _settings.multi_agent:
    try:
        _spec = PlanCompiler().compile(router_plan)
        _state = _build_graph_state(state, _settings)
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
    except Exception as exc:  # pragma: no cover
        LOGGER.exception(
            "graph execution failed, falling back to PTC: %s", exc,
        )
```

- [ ] **Step 10.4: Run full test suite (default OFF)**

```bash
.venv/bin/python -m pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 10.5: Smoke-test ON path (do NOT commit)**

```bash
TRADINGAGENTS_RUNTIME_MULTI_AGENT=true launchctl kickstart -k "gui/$(id -u)/com.tradingagents.web.venv"
```

Send a chat, tail:

```bash
tail -f /tmp/tradingagents-web-venv.log | grep "multi_agent: GraphExecutor"
```

Expected: heartbeat logs `starting (spec=N nodes, M edges, initial_hops=8, budget=5)` and `finished (hops_used=K, msgs=L)`. Reset:
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

- [ ] **Step 11.1: Run new tests + collision regression guard**

```bash
.venv/bin/python -m pytest \
    tests/test_agent_runtime_*.py \
    tests/test_multi_agent_*.py \
    tests/test_consult_subagent.py \
    -q
```

Expected: all pass. Explicitly running `test_agent_runtime_*.py` confirms no collision with the new sub-package.

- [ ] **Step 11.2: Run full repo test suite**

```bash
.venv/bin/python -m pytest tests/ -q
```

- [ ] **Step 11.3: Manual sanity**

- Start the service: `launchctl kickstart -k "gui/$(id -u)/com.tradingagents.web.venv"`
- Open `http://127.0.0.1:8000/`
- Send a chat; confirm `/reports` and `/scheduled` lists still render.
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
- [ ] `Edge.max_hops` defaults to **2** (spec §4.6 step 4)
- [ ] `Edge.hops_used` field exists and starts at 0 (per-edge loop counter)
- [ ] `resolve_ref()` handles `$agent.field`, `* N`, `? a : b`, comparison predicates, multi-op arithmetic with rightmost-split precedence (`$data.x * 2 + $data.y * 3 == 13` for x=2,y=3); 3-level nested lookup works (`$data.q.symbol.isin`); falls back to literal on failure
- [ ] `resolve_ref()` does NOT claim paren support (no test for `($a + $b) * 4`)
- [ ] `PlanCompiler` produces a `GraphSpec` for 1-group, 2-group, AND 3-group sequential cases
- [ ] `PlanCompiler` correctly maps `_TOOL_TO_AGENT` (including `command_resolver` and `trading_agents` agent-as-tool entries)
- [ ] **PlanCompiler's import is `from ...core.orchestrator import _TOOL_TO_AGENT` (3 dots, NOT 2)** — critical rev.4 fix; rev.3 plan's 2-dot version would crash at import
- [ ] `GraphExecutor.run()` walks a 2-node linear graph with `ToolNode`
- [ ] `GraphExecutor` populates downstream `state.inbox` for every edge fire (regression test `test_executor_populates_downstream_inbox`)
- [ ] `GraphExecutor` swallows `NotImplementedError` for `LLMNode`/`SubplanNode`/`ConsultNode` (logs warning, continues)
- [ ] `GraphExecutor` does NOT consume `llm_used` for stubbed LLMNodes
- [ ] `GraphExecutor` emits `multi_agent: GraphExecutor starting/finished` heartbeat logs (uses `initial_hops - state.hops_remaining`, NOT hardcoded `8`)
- [ ] **Loop edge fires upstream at most `max_hops` times via `Edge.hops_used` counter** (regression test `test_executor_loop_edge_re_executes_upstream`; rev.4 fix from HIGH #2/3/4)
- [ ] Global `state.hops_remaining` decrements ONCE per while-body iteration (NOT per-edge; rev.4 fix from HIGH #2)
- [ ] `_enqueue` uses `node.kind.value` for priority
- [ ] `consult_subagent` stub exists; second param named `context` and typed `ToolContext | None = None` (matches all other built-ins); raises `NotImplementedError`; NOT registered yet (Phase 2)
- [ ] `ConsultNode.outputs` is typed `list[FieldRef]` (rev.4 fix from LOW #12)
- [ ] Default flag OFF → PTC path identical to before (all existing tests pass)
- [ ] `multi_agent=true` flag → GraphExecutor runs (heartbeat confirmed in logs)
- [ ] `_build_graph_state(orch_state, settings)` reads `orch_state.intent.value`, NOT `router_plan.intent`; takes 2 args, not 3 (rev.4 fix)
- [ ] No dead `_program = _plan_from_router(router_plan, state)` call in wiring branch
- [ ] All existing tests pass (`pytest tests/ -q`)
- [ ] `tests/test_agent_runtime_*.py` explicitly pass (collision regression guard)
- [ ] `/reports` and `/scheduled` pages still render (regression guard)
- [ ] Phase 1 tag pushed to `tradingagentsplus`

## Out of Scope (deferred to later phases)

- Phase 2: real LLM call in `LLMNode` + `ConsultNode`; consult_subagent registered via `@tool_registry.register(...)` on the per-harness `ToolRegistry`; **add `consult_subagent` to `_TOOL_TO_AGENT` mapping** (e.g. → `consult_agent` or `router`); enforce `state.consultation_rate_limit` inside `GraphExecutor._invoke` (no nested `consult_subagent` calls; spec §7 LLM cost risk)
- Phase 3: cross-agent `$ref` resolution at runtime (in `ToolNode.run`); LLM router teaches `$ref` syntax; **PlanCompiler scans `call.args` for `$ref` expressions and emits data edges from referenced agent's output** (spec §4.6 step 2); **PlanCompiler enforces per-agent scoping — `$ref` without an explicit edge from the calling node raises `CompileError`** (spec §4.5); paren support in resolver (only if needed)
- Phase 4: `SubplanNode.run` + `trading_agents` graph fragment; `GraphState.fork()` lands; **`symbols` propagation into `GraphState`** (per spec §4.1); **`plan_id` propagation into `GraphState`** (per spec §4.1); orchestration nodes (`validate_inputs`, `verify_outputs`, `aggregate_signals`) added by `PlanCompiler` per spec §4.6; verifier reads `TypedResult.meta.source_ts` to reject stale outputs (spec §7)
- Phase 5: cutover — `multi_agent=true` becomes default
- Phase 5+: UI SSE events `agent_message` / `agent_handoff` and dock data-flow rows (spec §7); PlanCompiler cache key `(intent, agent_set, plan_hash)` keyed off `GraphSpec.to_dict()` (spec §4.6)
- Phase 6: mid-flight `GraphState` snapshots via `AgentRuntimeStore` (existing `tradingagents/agent_harness/runtime/store.py`)
- Phase 2 cleanup (optional): lift `_TOOL_TO_AGENT` from `core.orchestrator` into `agents/registry.py` or new `agents/tool_to_agent.py`

## Review Fixes Applied (rev. 1 → rev. 2 → rev. 3 → rev. 4)

| ID | Rev. | Fix |
|---|---|---|
| **B1** Package collision | r1 | New sub-package `runtime/multi_agent/` |
| **B2** Design overlap | r1 | "Relationship to existing PlanGraph" section |
| **B3** Test dir collision | r1 | Flat `tests/test_multi_agent_*.py` |
| **B4** No `pytest-asyncio` | r1 | `asyncio.run()` + `_run(coro)` |
| **B5** `ToolCall` no `depends_on` | r1 | Group-order deps |
| **B6** Wrong config location | r1 | `default_config.py` + env override (dotted-path) |
| **H1** ToolPipeline signature | r1 | Correct `await pipeline.run(...)` |
| **H2** Tool registration API | r1 | Phase 1 doesn't register; Phase 2 wires `@tool_registry.register(...)` |
| **H3** Edge validation | r1 | `__post_init__` validates `kind` |
| **H4** Protocol in Pydantic | r1 | `GraphSpec` dataclass + `to_dict()` |
| **H5** `_TOOL_TO_AGENT` coupling | r1 | Documented |
| **H6** Undefined helpers | r1 | `_build_graph_state` defined |
| **H7** Rollback semantics | r1 | `CompileError` + generic `Exception` both fall through |
| **M2** Premature `fork()` | r1 | Removed |
| **M5** Test naming | r1 | Prefixed `test_multi_agent_*` |
| **M8** `_TOOL_TO_AGENT` coverage | r1 | Test covers agent-as-tool entries |
| **L5** Chunk ordering | r1 | Compiler moved to Chunk 3 |
| **BLOCKER #1** (r2) `_build_graph_state.intent` | r3 | Reads `orch_state.intent.value` |
| **BLOCKER #2** (r2) Executor doesn't populate inbox | r3 | Fan-out calls `state.append_inbox(new_msg)` |
| **BLOCKER #3** (r2) Resolver precedence | r3 | Rightmost-split precedence |
| **HIGH #4** (r2) Wrong file path for env overrides | r3 | All references to `default_config.py` |
| **HIGH #5** (r2) Hand-waved dotted-path | r3 | `_set_dotted` + `_lookup_dotted` + env-var test |
| **HIGH #6** (r2) `consult_subagent` ctx→context | r3 | Param renamed |
| **HIGH #7** (r2) Dead `_program = _plan_from_router(...)` | r3 | Removed |
| **MEDIUM #8** (r2) `llm_used` before call | r3 | After `await node.run(...)` |
| **MEDIUM #9** (r2) Smoke test can't verify execution | r3 | Heartbeat logs + test |
| **MEDIUM #10** (r2) Priority uses `msg.kind` | r3 | `_enqueue` takes `source_node` |
| **MEDIUM #11** (r2) `_TOOL_TO_AGENT` coupling aspirational | r3 | Committed to `core.orchestrator` for Phase 1 |
| **LOW #12** (r2) `_NodeBase` parallel hierarchy | r3 | Removed |
| **LOW #13** (r2) `consult_subagent` not in `_TOOL_TO_AGENT` | r3 | Out of Scope (Phase 2) |
| **LOW #14** (r2) `harness.py:82` off-by-one | r3 | Drop line ref |
| **INFO #15** (r2) `symbols`/`plan_id` not in GraphState | r3 | Out of Scope (Phase 4) |
| **INFO #16** (r2) Spec §4.6 orchestration nodes | r3 | Out of Scope (Phase 4) |
| **HIGH #1** (r3) Wrong relative import `..core.orchestrator` | **r4** | Changed to **`...core.orchestrator`** (3 dots) |
| **HIGH #2** (r3) Loop hop double-decrement | **r4** | Removed per-edge decrement; global counter decrements once per iteration |
| **HIGH #3** (r3) `edge.max_hops` unreachable | **r4** | Added `Edge.hops_used` field; gate loop fires on per-edge counter |
| **HIGH #4** (r3) No loop-edge regression test | **r4** | `test_executor_loop_edge_re_executes_upstream` added |
| **HIGH #5** (r3) `test_executor_walks_linear_graph` assertion wrong | **r4** | Changed `>= 2` to `== 1` (only one cross-edge msg in linear graph) |
| **HIGH #6** (r3) Resolver paren test fails | **r4** | Dropped paren assertion; no paren support in Phase 1 |
| **MEDIUM #7** (r3) Hardcoded `8` in heartbeat | **r4** | Use `initial_hops - state.hops_remaining` |
| **MEDIUM #8** (r3) Cross-agent `$ref` scoping not tracked | **r4** | Out of Scope (Phase 3) |
| **MEDIUM #9** (r3) `plan_id` missing from Out of Scope | **r4** | Out of Scope entry |
| **MEDIUM #10** (r3) `consultation_rate_limit` dead in Phase 1 | **r4** | Out of Scope (Phase 2); docstring notes dead field |
| **MEDIUM #11** (r3) Per-edge hop accounting | **r4** | Addressed by HIGH #3 fix |
| **LOW #12** (r3) `ConsultNode.outputs` typo `list[list[FieldRef]]` | **r4** | Fixed to `list[FieldRef]` |
| **LOW #13** (r3) `consult_subagent` `dict[str, Any]` | **r4** | Changed to `ToolContext \| None = None` |
| **LOW #14** (r3) `Edge.max_hops` default 3 vs spec 2 | **r4** | Changed to `2` |
| **LOW #15** (r3) Missing 3-level `$ref` test | **r4** | `test_resolve_ref_3_level_nested_lookup` added |
| **LOW #16** (r3) Missing parse_ref edge-case tests | **r4** | `test_parse_ref_rejects_malformed` parametrized |
| **LOW #17** (r3) `_build_graph_state` unused param | **r4** | Dropped `router_plan` param |
| **INFO #18** (r3) UI SSE not in Out of Scope | **r4** | Out of Scope (Phase 5+) |
| **INFO #19** (r3) `source_ts` not mandated | **r4** | Out of Scope (Phase 4); state.py docstring notes reserved keys |
| **INFO #20** (r3) PlanCompiler caching layer | **r4** | Out of Scope (Phase 5+) |
