# [Phase 1 — Multi-Agent Runtime Skeleton] Implementation Plan (rev. 7)

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Last review round:** 6 (Aquinas). Verdict: BLOCK — 2 BLOCKERs introduced by rev.6 HIGH #1; both now resolved in rev.7 (1 HIGH + 4 MEDIUM + 2 LOW + 2 INFO). HIGH #1 introduces `tests/test_multi_agent_orchestrator_wiring.py`; verify this file exists in the test list before starting Phase 1.

**Goal:** Land the type system, `$ref` resolver (rightmost-split precedence, no parens), `PlanCompiler` (group-order case), `GraphExecutor` skeleton (inbox propagation + per-edge hop reset, no cross-run state leak), and `consult_subagent` stub **behind the `runtime.multi_agent` flag (default off)** without changing any existing behaviour. All current tests must remain green.

**Architecture:** All new code lives under a NEW sub-package `tradingagents/agent_harness/runtime/multi_agent/` to avoid colliding with the existing supervised AgentRuntime module (`AgentRuntime`, `AgentRuntimeStore`, `PlanGraph`, `GraphPatch`, etc.). The orchestrator gains a new code path gated by `runtime.multi_agent`; the existing PTC branch is untouched in this phase.

**Critical Phase 1 contracts** (locked in rev. 5):
- Executor MUST reset every `Edge.hops_used` to 0 at the top of `run()` so the same executor+spec pair can run multiple times. (Spec §4.7 expects per-state counters via `state.hops_for(edge)`; Phase 1 deviates by mutating the input `Edge` for simplicity — Phase 2 will move the counter onto `GraphState`.)
- Executor MUST populate `state.inbox` for downstream nodes whenever it fires an edge. (Spec §4.7.)
- Global `state.hops_remaining` decrements ONCE per while-body iteration, NOT per-edge.
- `Edge.max_hops` default is **2** (spec §4.6 step 4).
- `consult_subagent`'s second param is `context: ToolContext | None = None` (matches every other built-in tool's signature).
- Compiler's import of `_TOOL_TO_AGENT` is **LAZY** (inside `PlanCompiler.compile()`, not at module top) per rev.7 BLOCKER 2 (Aquinas round-6) — top-level import creates a circular dependency (orchestrator → multi_agent → compiler → orchestrator). See `core/orchestrator.py:2947` for an existing documention of this class of bug.
- Orchestrator's `PlanCompiler`/`GraphExecutor`/`load_settings` imports are also LAZY via `_lazy_multi_agent()` helper for the same reason.
- Resolver regex field capture requires leading `[A-Za-z_]` (matches Python attribute rules; rev.5 fix from LOW #8).
 - **Spec §4.4 vs §4.6 max_hops inconsistency**: §4.4 says loop edge default 3, §4.6 step 4 says 2 (Phase 1 uses 2 per §4.6; rev.3 LOW #14). Future spec reconciliation to 3 affects `test_edge_default_max_hops_is_2_per_spec` + loop regression test (`max_hops=2 → 4 message_log entries`).
 - **Phase 1 dispatch placement**: multi_agent branch lives in `_plan()` (planner) and runs ALONGSIDE PTC for observability only. Phase 5 (cutover) will move dispatch into `_execute()` and skip PTC (rev.6 INFO #2 / Pauli round-5).
- All `$ref` tests use rightmost-split precedence; **parentheses are NOT supported in Phase 1**.

**Relationship to existing PlanGraph** (resolves review blocker F2):
- `PlanGraph` (existing, supervised V2 runtime): run-level DAG — Tasks, dependencies, lifecycle states, persistence. Long-lived (hours to days).
- `GraphSpec` (new, Phase 1): turn-level ephemeral message-passing graph for multi-agent coordination within a single turn. Short-lived (seconds).
- The two are **complementary, not competing**. Phase 6 will persist mid-flight `GraphState` snapshots into the existing `AgentRuntimeStore` for crash recovery; Phase 1 only persists in memory.

**Tech Stack:** Python 3.14.6, asyncio, dataclasses (NOT Pydantic for GraphSpec/Edge), `pytest>=8.0` with `asyncio.run()` wrapper (no `pytest-asyncio` in this codebase).

**Spec:** `docs/superpowers/specs/2026-09-24-multi-agent-message-passing-runtime.md`

**Locked decisions (spec §8):**
- Q1=A declared inputs only
- Q2=A re-execute upstream node on loop trigger (hop index for staleness)
- Q3=B consultation cap configurable 0.0–0.8 (default 0.5)
- Q4=B all agents nestable
- Q5=B mid-flight state persisted via `AgentRuntimeStore` (lands in Phase 6)

---

## File Structure (revised to avoid collisions)

### New files (all under `tradingagents/agent_harness/runtime/multi_agent/`)

| File | Lines | Responsibility |
|---|---|---|
| `__init__.py` | ~30 | Public exports |
| `settings.py` | ~30 | `RuntimeSettings` (Pydantic) + `load_settings()` factory |
| `state.py` | ~120 | `GraphState`, `TypedResult`, `Message`, `FieldRef` |
| `graph.py` | ~140 | `NodeKind`, `BaseNode`, `Edge` (with `hops_used` — reset by executor per run), `GraphSpec` |
| `resolver.py` | ~180 | `parse_ref`, `is_ref_expr`, `resolve_ref` (rightmost-split precedence; no parens; field regex requires `[A-Za-z_]` start) |
| `nodes.py` | ~200 | `ToolNode` + stubs; correct `list[FieldRef]` typing |
| `compiler.py` | ~180 | `PlanCompiler`; **`from ...core.orchestrator` (3 dots)** |
| `executor.py` | ~310 | `GraphExecutor`; per-run `Edge.hops_used` reset; inbox propagation; heartbeat logs |

### Modified files

| File | Change |
|---|---|
| `tradingagents/default_config.py` | `runtime.*` block + 4 env-override entries + `_set_dotted` / `_lookup_dotted` |
| `tradingagents/agent_harness/core/orchestrator.py` | Flag-gated GraphExecutor dispatch; `_build_graph_state(orch_state, settings)` |

### New tool

| File | Lines | Responsibility |
|---|---|---|
| `tradingagents/agent_harness/tools/builtin_consult.py` | ~80 | `ConsultSubagentArgs` + `consult_subagent` async fn; **`context: ToolContext \| None = None`** |

### New test files (flat in `tests/`)

| File | Coverage |
|---|---|
| `tests/test_multi_agent_settings.py` | Defaults + override + env var injection |
| `tests/test_multi_agent_state.py` | State dataclass round-trip |
| `tests/test_multi_agent_graph.py` | `GraphSpec` + `Edge` + `to_dict()` |
| `tests/test_multi_agent_resolver.py` | `$ref` parser + evaluator; NO parens; field regex |
| `tests/test_multi_agent_compiler.py` | 1/2/3-group + agent-as-tool mapping |
| `tests/test_multi_agent_nodes.py` | `ToolNode` + stubs |
| `tests/test_multi_agent_executor.py` | Linear + inbox + budget + heartbeat + **loop-edge** + **cross-run reset** |
| `tests/test_consult_subagent.py` | Args validation + signature |
| `tests/test_multi_agent_orchestrator_wiring.py` | Flag-off skips multi_agent; flag-on runs PlanCompiler + GraphExecutor; flag-on falls through to PTC on `CompileError` + generic `Exception` (rev.6 HIGH #1 / Pauli round-5) |

All tests use `asyncio.run()` + `_run(coro)` helper. **No bare `async def test_...`**.

---

## Chunk 1: Settings + Types (no behavior change, just shape)

### Task 1: `RuntimeSettings` + config wiring (dotted-path + env var injection)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/settings.py`
- Modify: `tradingagents/default_config.py`
- Test: `tests/test_multi_agent_settings.py`

- [ ] **Step 1.1: Write failing test**

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

- [ ] **Step 1.5: Add env overrides + `_set_dotted` helper**

In `tradingagents/default_config.py`, add to `_ENV_OVERRIDES`:

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

Rev.5 contract: ``TypedResult.meta`` reserves three keys per spec §7:
``source_ts`` (staleness), ``source_agent``, ``source_tool``. Phase 4 verifier
reads ``source_ts`` to reject stale outputs. Phase 1 producers SHOULD populate
``source_ts`` but it's not enforced.

Rev.5 bootstrap doc: ``state.inbox[entry]`` is **empty** after bootstrap
(see executor.py docstring). Entry nodes must use ``self.raw_args`` for Phase 1
input. Phase 3 will wire ``\$ref`` resolution; entry-node ``\$ref`` may produce
stale/missing inputs until ``state.agent_outputs`` population lands.
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
    # NOTE: spec §4.1 also lists `symbols` and `plan_id`; both land in Phase 4.
    agent_outputs: dict[str, TypedResult] = field(default_factory=dict)
    inbox: dict[str, list[Message]] = field(default_factory=dict)
    message_log: list[Message] = field(default_factory=list)
    hops_remaining: int = 8
    llm_used: int = 0
    consultation_used: int = 0
    # NOTE: `consultation_rate_limit` is dead in Phase 1 — Phase 2 enforces
    # the "no nested consult" rule inside GraphExecutor._invoke (Out of Scope).
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

### Task 3: `NodeKind` + `BaseNode` + `Edge` (with `hops_used` reset by executor) + `GraphSpec`

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

Edge validates `kind` in __post_init__ and tracks `hops_used` for per-edge
loop accounting. The GraphExecutor resets every edge's `hops_used = 0` at
the top of each `run()` so the same executor+spec pair can run multiple
times (rev.5 fix from round-4 HIGH #1).

`max_hops` default is 2 per spec §4.6 step 4.

Rev.5: ``GraphSpec.to_dict()`` MUST be derived from immutable fields only —
do NOT include ``Edge.hops_used`` in any future cache key (see MEDIUM #2).
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
    hops_used: int = 0  # executor resets to 0 at top of each run()

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
        """JSON-serializable snapshot.

        NOTE (rev.5 MEDIUM #2): ``hops_used`` is mutable state mutated by the
        executor. Do NOT use ``to_dict()`` as a cache key — derive cache keys
        from immutable parts only (intent, frozenset(nodes), plan source hash).
        """
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
git commit -m "feat(runtime): §0.4.35 phase 1 — Edge with hops_used, GraphSpec (to_dict docstring)"
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

### Task 5: `$ref` parser + evaluator (rightmost-split precedence, NO parens, field regex tightened)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/resolver.py`
- Test: `tests/test_multi_agent_resolver.py`

> **Rev.5 fix (LOW #8)**: Field regex tightened to require `[A-Za-z_]` start, matching the agent-name capture and Python attribute rules. `$data.0field` now FAILS to parse (consistent with dataclass lookups).
>
> **No parens in Phase 1**. Rewrite expressions like `($a + $b) * 4` to `$a * 4 + $b * 4`.

- [ ] **Step 5.1: Write failing tests**

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
    assert parse_ref("\$data.q.symbol.isin") == ("data", "q.symbol.isin")

def test_parse_ref_no_dollar_returns_none():
    assert parse_ref("data.quote.symbol") is None

@pytest.mark.parametrize("bad_input,reason", [
    ("\$\$",          "no agent name after first dollar"),
    ("\$abc",         "no dot in regex group"),
    ("\$data.",       "empty field name"),
    ("\$data.0field", "field must start with letter or underscore"),
    ("",              "empty string"),
])
def test_parse_ref_rejects_malformed(bad_input, reason):
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
    # $data.x=2, $data.y=3 → 2*2 + 3*3 == 13
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

Rev.5 (LOW #8): field regex tightened to require `[A-Za-z_]` start. This
matches the agent-name capture and Python attribute-name rules. ``\$data.0field``
fails to parse (consistent with dataclass lookups where ``0field`` is not a
valid attribute name).
"""
from __future__ import annotations
import re
from typing import Any, Optional

from .state import GraphState

# rev.5: field must start with letter or underscore (matches agent capture + Python attrs)
_REF_RE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_.]*)")

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
git commit -m "feat(runtime): §0.4.35 phase 1 — \$ref resolver (tightened field regex, no parens)"
```

### Task 6: `consult_subagent` tool stub (`context: ToolContext | None`)

**Files:**
- Create: `tradingagents/agent_harness/tools/builtin_consult.py`
- Test: `tests/test_consult_subagent.py`

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

Rev.5 contract: the ``context`` parameter uses the standard ToolContext type
(defaults to None), matching every other built-in tool in ``tools/builtin.py``.
Renaming or retyping this in Phase 2 will break callers, so the Phase 1 stub
locks the contract.
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
import time
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
        # Phase 4 verifier reads source_ts; populate it now per spec §7.
        typed = TypedResult(
            schema=dict, data=result,
            meta={"tool": self.tool_name, "source_ts": time.monotonic(),
                  "source_agent": self.agent_id},
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
        self.inputs: list[list[FieldRef]] = list(inputs or [])
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

- [ ] **Step 7.4: Run, expect PASS**

- [ ] **Step 7.5: Commit**

```bash
git add tradingagents/agent_harness/runtime/multi_agent/nodes.py \
        tests/test_multi_agent_nodes.py
git commit -m "feat(runtime): §0.4.35 phase 1 — ToolNode + stubs (FieldRef typing, source_ts)"
```

### Task 8: `PlanCompiler` (group-order deps, **3-dot relative import**)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/compiler.py`
- Test: `tests/test_multi_agent_compiler.py`

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

- [ ] **Step 8.2: Run, expect FAIL**

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
#
# Rev.7 BLOCKER 2 (Aquinas round-6): `_TOOL_TO_AGENT` (defined at line 660 of
# core/orchestrator.py) MUST be imported LAZILY inside `PlanCompiler.compile()`,
# not at module top. Loading `compiler.py` at orchestrator-import time would
# trigger a circular import (orchestrator → multi_agent → compiler → orchestrator).
# orchestrator.py:2947 already documents this class of bug.
_TOOL_TO_AGENT = None  # lazily resolved on first compile() call

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

        # Rev.7 BLOCKER 2: lazy import of _TOOL_TO_AGENT (avoids circular import).
        global _TOOL_TO_AGENT
        if _TOOL_TO_AGENT is None:
            from ...core.orchestrator import _TOOL_TO_AGENT as _t2a
            _TOOL_TO_AGENT = _t2a

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

### Task 9: `GraphExecutor` (inbox propagation + per-run hop reset + heartbeat + cross-run safe)

**Files:**
- Create: `tradingagents/agent_harness/runtime/multi_agent/executor.py`
- Test: `tests/test_multi_agent_executor.py`

> **Rev.5 contracts**:
> - **HIGH #1**: reset every `Edge.hops_used = 0` at top of `run()` (so executor+spec can run multiple times).
> - **HIGH #2** (rev.4): drop double-decrement; global counter decrements ONCE per iteration.
> - **HIGH #3** (rev.4): per-edge `Edge.hops_used` gates loop fires.
> - **HIGH #5** (rev.4): linear graph test asserts `== 1` message_log entry (only 1 cross-edge).
> - **MEDIUM #7** (rev.4): heartbeat log uses `initial_hops - state.hops_remaining`, not hardcoded `8`.
> - **LOW #6**: rename `_enqueue` param `source_node` → `dest_node` (it's actually the destination).
> - **LOW #7**: reset `self._seq = 0` at top of `run()` for clean per-run isolation.
> - **HIGH #4** (rev.4): loop test with `assert == 4` (4 messages: initial fan-out + 2 loop fires × 2 msgs each).
> - **NEW (rev.5)**: cross-run regression test `test_executor_resets_hops_between_runs`.

- [ ] **Step 9.1: Write failing tests (6 cases)**

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
                  tool_name="get_quote", raw_args={"{"}": "X"}.replace("{}", "symbol") if False else {"symbol": "X"})
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
    """regression for round-2 BLOCKER #2."""
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
    assert out.llm_used == 0


def test_executor_loop_edge_re_executes_upstream(monkeypatch):
    """rev.4/5 regression for loop edges: max_hops=2 → loop fires twice,
    each fire creates 2 msgs (new_msg + loop_msg). Total = 1 (initial) + 4
    (2 fires × 2 msgs) = 4 messages in message_log.

    Trace:
    - iter 1 (hops8→7): pop bootstrap. Run → 1 out_msg. Loop fires (hops_used0→1).
      append_inbox(new_msg_1), append_inbox(loop_msg_1). Log=2.
    - iter 2 (hops 7→6): pop new_msg_1. Run → 1 out_msg. Loop fires (hops_used 1→2).
      append_inbox(new_msg_2), append_inbox(loop_msg_2). Log=4.
    - iters 3-5: pop remaining queued msgs; runs happen but no more loop fires.
    """
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    spec = _graph_self_loop()
    state = GraphState(run_id="r", turn_id="t", intent="x")
    _run(GraphExecutor().run(spec, state))

    assert len(state.message_log) == 4, (
        f"expected 4 messages (initial fan-out + 2 loop fires × 2 msgs); "
        f"got {len(state.message_log)}"
    )


def test_executor_resets_hops_between_runs(monkeypatch):
    """rev.5 regression for round-4 HIGH #1: same executor+spec pair must
    produce identical results on consecutive runs (no Edge.hops_used leak)."""
    from tradingagents.agent_harness.runtime.multi_agent import nodes as nodes_mod

    class FakePipeline:
        async def run(self, *, tool_name, args, tool_context, executor):
            return MagicMock(ok=True, result={"echoed": tool_name})

    monkeypatch.setattr(nodes_mod, "_default_pipeline", lambda: FakePipeline())

    spec = _graph_self_loop()
    state1 = GraphState(run_id="r1", turn_id="t1", intent="x")
    state2 = GraphState(run_id="r2", turn_id="t2", intent="x")
    executor = GraphExecutor()

    _run(executor.run(spec, state1))
    log_after_first = len(state1.message_log)

    _run(executor.run(spec, state2))
    log_after_second = len(state2.message_log)

    assert log_after_first == log_after_second, (
        f"second run produced {log_after_second} messages vs first run's "
        f"{log_after_first}; Edge.hops_used leaked across runs"
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

- [ ] **Step 9.2: Run, expect FAIL**

- [ ] **Step 9.3: Implement `executor.py`**

```python
# tradingagents/agent_harness/runtime/multi_agent/executor.py
"""Phase 1 GraphExecutor skeleton.

Walks a GraphSpec using a priority queue. Honours budget + hop guards.

Rev.5 contracts:
- Resets every ``Edge.hops_used = 0`` at the top of each ``run()`` so the
  same executor+spec pair can run multiple times.
- Every edge fire calls ``state.append_inbox(new_msg)`` so downstream
  ``consume_inbox(receiver)`` sees upstream outputs (spec §4.7).
- Global ``state.hops_remaining`` decrements ONCE per while-body iteration
  (NOT per-edge). Per-edge loop accounting uses ``Edge.hops_used``.
- Stubbed LLMNode does NOT consume budget — increment happens AFTER
  ``await node.run(...)``.
- Heartbeat logs use ``initial_hops - state.hops_remaining``.
- ``_enqueue`` parameter ``dest_node`` is the DESTINATION node (its kind
  determines heapq priority), not the source.
- ``self._seq`` resets to 0 at top of ``run()`` for clean per-run isolation.

Phase 1 deviation from spec §4.7: spec uses ``state.hops_for(edge)`` (per-state
counter); Phase 1 mutates the input ``Edge.hops_used``. Phase 2 will move the
counter onto ``GraphState`` (rev.5 HIGH #1 follow-up).
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
        # rev.5: per-run reset (round-4 HIGH #1)
        for edge in spec.edges:
            edge.hops_used = 0
        self._seq = 0  # rev.5 LOW #7: clean per-run isolation

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

        # Bootstrap: enqueue entry activation. Note that the bootstrap msg
        # is NOT deposited into state.inbox[entry] — entry nodes must use
        # self.raw_args for Phase 1 input. Phase 3 will wire $ref resolution
        # and state.agent_outputs population (see state.py docstring).
        queue: list[_Pending] = []
        self._enqueue(
            queue, entry,
            Message(sender="__start__", receiver=entry,
                    payload=TypedResult(schema=dict, data={}, meta={})),
            dest_node=spec.node(entry),
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
                        dest_node=spec.node(edge.dst),
                    )
                    if edge.kind == "loop":
                        # rev.4: per-edge counter, NOT global
                        edge.hops_used += 1
                        loop_msg = Message(
                            sender=node.id, receiver=edge.src,
                            payload=m.payload, kind=m.kind, hop=m.hop + 1,
                        )
                        state.append_inbox(loop_msg)
                        self._enqueue(
                            queue, edge.src, loop_msg,
                            dest_node=spec.node(edge.src),
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
            return edge.hops_used < edge.max_hops
        return False

    def _enqueue(self, queue: list[_Pending], receiver: str,
                 msg: Message, *, dest_node: Optional[BaseNode] = None) -> None:
        # rev.5 LOW #6: parameter is the DESTINATION node (its kind drives
        # priority). Previously named `source_node` which was misleading.
        if dest_node is not None:
            prio = _PRIORITY.get(dest_node.kind.value, 5)
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
git commit -m "feat(runtime): §0.4.35 phase 1 — GraphExecutor (per-run reset, cross-run safe)"
```

### Task 9.5: Create `tests/test_multi_agent_orchestrator_wiring.py` (BLOCKER 1, rev.7)

**Files:**
- Create: `tests/test_multi_agent_orchestrator_wiring.py`

This file is referenced 4 times in rev.6 but no Task created it (Aquinas round-6 BLOCKER 1). **Phase 1 ships with `multi_agent=False` as default; without these three tests, the flag-gated dispatch has no regression guard.**

- [ ] **Step 9.5.1: Write the three tests**

```python
# tests/test_multi_agent_orchestrator_wiring.py
import asyncio
from unittest.mock import AsyncMock, MagicMock

from tradingagents.default_config import cfg
from tradingagents.agent_harness.core import orchestrator
from tradingagents.agent_harness.runtime.multi_agent import CompileError


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_orchestrator_flag_off_skips_multi_agent(monkeypatch):
    cfg.set_config({"runtime": {"multi_agent": False}})

    compile_called = []
    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.PlanCompiler.compile",
        lambda self, plan: compile_called.append(plan) or _stub_spec(),
    )
    run_mock = AsyncMock(side_effect=AssertionError("should not run when flag is off"))
    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.GraphExecutor.run",
        run_mock,
    )

    # Invoke _plan() with a minimal router_plan — assert compile_called == [].
    _run(orchestrator._plan(...))
    assert compile_called == [], "flag-off must skip PlanCompiler"


def test_orchestrator_flag_on_runs_multi_agent(monkeypatch):
    cfg.set_config({"runtime": {"multi_agent": True}})

    compile_called = []
    run_called = []

    class _StubCompiler:
        def compile(self, plan):
            compile_called.append(plan)
            return _stub_spec()

    class _StubExecutor:
        def __init__(self, *a, **kw):
            pass

        async def run(self, spec, state):
            run_called.append((spec, state))
            return state

    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.PlanCompiler",
        _StubCompiler,
    )
    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.GraphExecutor",
        _StubExecutor,
    )

    _run(orchestrator._plan(...))
    assert len(compile_called) == 1
    assert len(run_called) == 1


def test_orchestrator_flag_on_falls_through_on_compile_error(monkeypatch, caplog):
    cfg.set_config({"runtime": {"multi_agent": True}})

    class _BoomCompiler:
        def compile(self, plan):
            raise CompileError("boom")

    monkeypatch.setattr(
        "tradingagents.agent_harness.runtime.multi_agent.PlanCompiler",
        _BoomCompiler,
    )

    # _plan() must NOT raise — fall through to PTC.
    import logging
    caplog.set_level(logging.WARNING)
    _run(orchestrator._plan(...))
    assert any(
        "graph compile failed, falling back to PTC" in rec.message
        for rec in caplog.records
    ), "CompileError must log a fall-through warning"


def _stub_spec():
    from tradingagents.agent_harness.runtime.multi_agent.graph import GraphSpec
    return GraphSpec(nodes={}, edges=[], entry=None, terminals=set(), metadata={})
```

- [ ] **Step 9.5.2: Commit**

```bash
git add tests/test_multi_agent_orchestrator_wiring.py
git commit -m "test(runtime): §0.4.35 phase 1 — orchestrator flag-off/on/fall-through (rev.7 BLOCKER 1)"
```

---

### Task 10: Wire executor into orchestrator (helper takes `(orch_state, settings)`)

**Files:**
- Modify: `tradingagents/agent_harness/runtime/multi_agent/__init__.py` (expand exports)
- Modify: `tradingagents/agent_harness/core/orchestrator.py`

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
# Rev.7 BLOCKER 2 (Aquinas round-6): MUST import lazily INSIDE the dispatch
# branch, not at module top, to avoid circular import
# (orchestrator → multi_agent → compiler → orchestrator).
# The symbols are resolved on first dispatch call only.
```

After the existing import section, define a lazy-resolver helper:

```python
def _lazy_multi_agent():
    from ..runtime.multi_agent import (
        GraphExecutor as _GE, PlanCompiler as _PC,
        CompileError as _CE, load_settings as _ls,
    )
    return _GE, _PC, _CE, _ls
```

Add helper near other `_build_*` helpers:

```python
def _build_graph_state(orch_state, settings):
    """Build a per-turn GraphState from the orchestrator's state.

    Phase 1: shallow — only carries run_id / turn_id / intent / budget knobs.
    Phase 6 will wire AgentRuntimeStore snapshot loading.

    Rev.4: reads ``orch_state.intent.value`` (RouterPlan has no ``intent``
    field — see round-2 BLOCKER #1).
    Rev.5: helper signature is (orch_state, settings); ``router_plan`` was
    unused (LOW #17).
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
# Rev.7 BLOCKER 2 (Aquinas round-6): lazy resolver — top-level import would
# trigger circular import (orchestrator → multi_agent → compiler → orchestrator).
GraphExecutor, PlanCompiler, CompileError, load_settings = _lazy_multi_agent()
try:
    _settings = load_settings()
except Exception as exc:  # pragma: no cover — Rev.7 MEDIUM #2: Pydantic ValidationError must fall through
    LOGGER.warning("runtime settings invalid, falling back to PTC: %s", exc)
    raise _ForcePTCFallback() from exc
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

- [ ] **Step 10.5: Smoke-test ON path (do NOT commit)**

```bash
TRADINGAGENTS_RUNTIME_MULTI_AGENT=true launchctl kickstart -k "gui/$(id -u)/com.tradingagents.web.venv"
```

Send a chat, tail:
```bash
tail -f /tmp/tradingagents-web-venv.log | grep "multi_agent: GraphExecutor"
```

Reset:
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

- [ ] `RuntimeSettings` defaults match spec §4.9
- [ ] `TRADINGAGENTS_RUNTIME_MULTI_AGENT` env var toggles the flag
- [ ] `GraphState` / `TypedResult` / `Message` / `FieldRef` importable
- [ ] `GraphSpec` validates nodes + edges; `to_dict()` round-trips; docstring warns against using it as cache key (rev.5 MEDIUM #2)
- [ ] `Edge.max_hops` defaults to 2
- [ ] `resolve_ref()` handles `$agent.field` (3-level nested), `* N`, `? a : b`, comparison, multi-op arithmetic with rightmost-split precedence
- [ ] **`resolve_ref()` field regex requires `[A-Za-z_]` start** — `$data.0field` rejected (rev.5 LOW #8)
- [ ] `PlanCompiler` produces `GraphSpec` for 1/2/3-group sequential cases; import is `from ...core.orchestrator` (3 dots)
- [ ] `PlanCompiler` correctly maps `_TOOL_TO_AGENT` (incl. `command_resolver`, `trading_agents`)
- [ ] `GraphExecutor.run()` walks 2-node linear graph
- [ ] `GraphExecutor` populates downstream `state.inbox` for every edge fire
- [ ] `GraphExecutor` swallows `NotImplementedError` for stubbed nodes; doesn't consume budget
- [ ] `GraphExecutor` emits heartbeat logs using `initial_hops - state.hops_remaining` (not hardcoded `8`)
- [ ] **Loop edge fires upstream at most `max_hops` times via `Edge.hops_used`** (test asserts `== 4` message_log entries)
- [ ] **`GraphExecutor.run()` resets every `Edge.hops_used = 0` at start**; same executor+spec can run twice with identical output (test `test_executor_resets_hops_between_runs`)
- [ ] **`self._seq` resets to 0 at top of `run()`** (LOW #7)
- [ ] **`_enqueue` parameter renamed `source_node` → `dest_node`** (LOW #6)
- [ ] Global `state.hops_remaining` decrements ONCE per iteration
- [ ] `_enqueue` uses `node.kind.value` for priority
- [ ] `consult_subagent` stub: param `context: ToolContext | None = None`; raises `NotImplementedError`; NOT registered yet
- [ ] `ConsultNode.outputs` typed `list[FieldRef]`
- [ ] **`ConsultNode` stub does NOT consume `state.consultation_used`** (parallel to existing LLMNode test; rev.7 MEDIUM #4 / Aquinas round-6)
- [ ] `ToolNode.run` populates `TypedResult.meta` with `source_ts` and `source_agent`
- [ ] Default flag OFF → PTC path identical to before
- [ ] `multi_agent=true` flag → GraphExecutor runs (heartbeat confirmed in logs)
- [ ] `_build_graph_state(orch_state, settings)` reads `orch_state.intent.value`; 2 args
- [ ] No dead `_program = _plan_from_router(...)` call
- [ ] All existing tests pass
- [ ] `tests/test_agent_runtime_*.py` explicitly pass (collision regression guard)
- [ ] **`runtime.multi_agent=False` → orchestrator `_plan()` never invokes `PlanCompiler.compile` nor `GraphExecutor.run`** (test `test_orchestrator_flag_off_skips_multi_agent`, rev.6 HIGH #1)
- [ ] **`runtime.multi_agent=True` → orchestrator invokes `PlanCompiler.compile` once + `GraphExecutor.run` once** (test `test_orchestrator_flag_on_runs_multi_agent`, rev.6 HIGH #1)
- [ ] **`runtime.multi_agent=True` + compile/Executor raises `CompileError` (or any `Exception`) → orchestrator logs warning + falls through to PTC plan, no exception propagated** (test `test_orchestrator_flag_on_falls_through_on_compile_error`, rev.6 HIGH #1)
- [ ] `/reports` and `/scheduled` pages still render
- [ ] Phase 1 tag pushed to `tradingagentsplus`

## Out of Scope (deferred to later phases)

- **Phase 2**: real LLM call in `LLMNode` + `ConsultNode`; consult_subagent registered via `@tool_registry.register(...)`; **add `consult_subagent` to `_TOOL_TO_AGENT`** mapping; enforce `state.consultation_rate_limit` inside `GraphExecutor._invoke` (no nested `consult_subagent` calls; spec §7 LLM cost risk); **move per-edge loop hop counter from `Edge.hops_used` to `state.edge_hops: dict[(src,dst), int]`** per spec §4.7 (round-4 HIGH #1 follow-up — Phase 1 mutates input GraphSpec as a minimal patch)
- **Phase 3**: cross-agent `$ref` resolution at runtime (in `ToolNode.run`); LLM router teaches `$ref` syntax; **PlanCompiler scans `call.args` for `$ref` expressions and emits data edges from referenced agent's output** (spec §4.6 step 2); **PlanCompiler enforces per-agent scoping — `$ref` without an explicit edge from the calling node raises `CompileError`** (spec §4.5); **PlanCompiler detects feedback (an agent's `$ref` points to a prior group) and emits a loop edge with `max_hops=2` instead of a data edge** (spec §4.6 step 4); **after each node run, `GraphExecutor` writes `state.agent_outputs[node.id] = result`** so resolver's `_lookup` works for cross-agent refs (round-4 MEDIUM #4); paren support in resolver (only if needed); **entry-node `$ref` resolution may produce stale/missing inputs until `state.agent_outputs` population lands** (round-4 MEDIUM #5); **replace executor's `activated: set[str]` (`has-been-run` + `has_loop` bypass) with FieldRef-satisfaction check per spec §4.7 — clear semantics:
- Node WITH `$ref` inputs → activated when every FieldRef resolves to non-empty `state.agent_outputs`.
- Node WITHOUT `$ref` inputs (uses raw_args like Phase 1 ToolNodes) → activated UNCONDITIONALLY on message arrival.
- `has_loop` bypass is DROPPED in both cases — loop re-fire goes through `Edge.hops_used` plus message routing; loop edges still re-fire via per-edge counter.
** (rev.7 MEDIUM #1 / Aquinas round-6; supersedes rev.6 MEDIUM #1)
- **Phase 4**: `SubplanNode.run` + `trading_agents` graph fragment; `GraphState.fork()` lands; `symbols` + `plan_id` propagation into `GraphState`; orchestration nodes (`validate_inputs`, `verify_outputs`, `aggregate_signals`) added by `PlanCompiler` per spec §4.6; verifier reads `TypedResult.meta.source_ts` to reject stale outputs (spec §7)
- **Phase 5**: cutover — `multi_agent=true` becomes default; **decision pending on what happens to empty `router_plan` and PTC-fallback results — candidates are (a) silent legacy + graph runs for diagnostics, (b) skip graph entirely if router_plan empty, (c) graph + PTC run in parallel and synthesize** (rev.7 MEDIUM #3 / Aquinas round-6)
- **Phase 5+**: UI SSE events `agent_message` / `agent_handoff` and dock data-flow rows (spec §7); PlanCompiler cache key `(intent, agent_set, plan_hash)` derived from **immutable parts only** — NOT `GraphSpec.to_dict()` (round-4 MEDIUM #2; `to_dict()` includes mutable `hops_used`); a future cache helper should hash `(intent, frozenset(nodes), plan_source_hash)` where `plan_source_hash` is computed from the raw `RouterPlan` before any `Edge.hops_used` mutation
- **Phase 6**: mid-flight `GraphState` snapshots via `AgentRuntimeStore`
- **Phase 6+**: Spec §4.8 'GraphExecutor checks if it can flatten to PTC for speed' (rev.7 INFO #2 / Aquinas round-6) — evaluation-only optimization; never the source of truth
- **Phase 2 cleanup (optional)**: lift `_TOOL_TO_AGENT` from `core.orchestrator` into `agents/registry.py` or new `agents/tool_to_agent.py`

## Review Fixes Applied (rev. 1 → rev. 7)

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
| **H7** Rollback semantics | r1 | `CompileError` + `Exception` both fall through |
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
| **MEDIUM #10** (r2) Priority uses `msg.kind` | r3 | `_enqueue` takes `dest_node` |
| **MEDIUM #11** (r2) `_TOOL_TO_AGENT` coupling aspirational | r3 | Committed to `core.orchestrator` for Phase 1 |
| **LOW #12** (r2) `_NodeBase` parallel hierarchy | r3 | Removed |
| **LOW #13** (r2) `consult_subagent` not in `_TOOL_TO_AGENT` | r3 | Out of Scope (Phase 2) |
| **LOW #14** (r2) `harness.py:82` off-by-one | r3 | Drop line ref |
| **INFO #15** (r2) `symbols`/`plan_id` not in GraphState | r3 | Out of Scope (Phase 4) |
| **INFO #16** (r2) Spec §4.6 orchestration nodes | r3 | Out of Scope (Phase 4) |
| **HIGH #1** (r3) Wrong relative import | r4 | `from ...core.orchestrator` (3 dots) |
| **HIGH #2** (r3) Loop hop double-decrement | r4 | Per-edge `Edge.hops_used`; global counter once per iteration |
| **HIGH #3** (r3) `edge.max_hops` unreachable | r4 | `Edge.hops_used` field; gate loop fires on it |
| **HIGH #4** (r3) No loop-edge test | r4 | `test_executor_loop_edge_re_executes_upstream` |
| **HIGH #5** (r3) Linear graph assertion wrong | r4 | `== 1` (only 1 cross-edge msg) |
| **HIGH #6** (r3) Resolver paren test fails | r4 | Dropped paren assertion |
| **MEDIUM #7** (r3) Hardcoded `8` in heartbeat | r4 | `initial_hops - state.hops_remaining` |
| **MEDIUM #8** (r3) Cross-agent `$ref` scoping | r4 | Out of Scope (Phase 3) |
| **MEDIUM #9** (r3) `plan_id` missing | r4 | Out of Scope (Phase 4) |
| **MEDIUM #10** (r3) `consultation_rate_limit` dead | r4 | Out of Scope (Phase 2) |
| **MEDIUM #11** (r3) Per-edge hop accounting | r4 | Addressed by HIGH #3 |
| **LOW #12** (r3) `ConsultNode.outputs` typo | r4 | Fixed to `list[FieldRef]` |
| **LOW #13** (r3) `consult_subagent` `dict[str, Any]` | r4 | `ToolContext \| None = None` |
| **LOW #14** (r3) `Edge.max_hops` default 3 vs 2 | r4 | Changed to 2 |
| **LOW #15** (r3) Missing 3-level `$ref` test | r4 | Test added |
| **LOW #16** (r3) Missing `parse_ref` edge cases | r4 | Parametrized test |
| **LOW #17** (r3) `_build_graph_state` unused param | r4 | Dropped `router_plan` |
| **INFO #18** (r3) UI SSE not in Out of Scope | r4 | Out of Scope (Phase 5+) |
| **INFO #19** (r3) `source_ts` not mandated | r4 | Out of Scope (Phase 4) |
| **INFO #20** (r3) PlanCompiler caching layer | r4 | Out of Scope (Phase 5+) |
| **HIGH #1** (r4) `Edge.hops_used` leak across runs | **r5** | Reset every edge at top of `run()` + regression test |
| **MEDIUM #2** (r4) Cache key non-deterministic | **r5** | `GraphSpec.to_dict()` docstring warns against cache key use; Out of Scope entry |
| **MEDIUM #3** (r4) Feedback-loop detection missing | **r5** | Out of Scope (Phase 3) |
| **MEDIUM #4** (r4) `state.agent_outputs` never populated | **r5** | Out of Scope (Phase 3) |
| **MEDIUM #5** (r4) Bootstrap empty-inbox | **r5** | Docstring + Out of Scope (Phase 3) |
| **LOW #6** (r4) `_enqueue` param `source_node` misleading | **r5** | Renamed to `dest_node` |
| **LOW #7** (r4) `_seq` counter persists | **r5** | Reset at top of `run()` |
| **LOW #8** (r4) Resolver digit-start field | **r5** | Tightened regex; parametrized test |
| **INFO #9** (r4) Loop-test comment undercounts | **r5** | Tightened to `== 4`; comment updated |
