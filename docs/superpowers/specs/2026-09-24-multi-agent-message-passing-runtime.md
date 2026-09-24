# Multi-Agent Message-Passing Runtime — Design

**Date**: 2026-09-24
**Status**: Draft (pre-approval)
**Scope**: §0.4.35 — replace the current PTC executor with a LangGraph-style
message-passing runtime so subagents can pass data, invoke sub-plans, and
talk to each other through a typed message protocol.

## 1. Problem

Current harness dispatches tool calls. Every "subagent" is just a label
(`_TOOL_TO_AGENT`). The execution model is:

- LLM Router → flat list of `{tool, args, parallel_group}` calls
- PTC executor → topo-sort groups → asyncio.gather waves
- Each call goes through `tool.invoke()` (5-stage pipeline)
- Results are independent. There is no way for one subagent's result to
  become another's input without the LLM re-reasoning over raw text.

Three things missing:

1. **Data flow between subagents** — `data_agent` computes fundamentals;
   `alpha_agent` needs to know `pe_ratio` to pick factor weights. Today
   the synthesizer re-reads both results in the LLM. Wasted tokens, fragile.

2. **Sub-plan nesting** — `trading_agents` runs a multi-phase deep analysis
   internally. From the orchestrator's view it's one tool call. We can't
   observe, gate, or short-circuit its phases.

3. **Inter-agent dialogue** — a subagent can't ask another subagent a
   focused question. E.g. "data_agent, based on this quote, which alpha
   factors are likely to be informative?" Today, only the synthesizer
   LLM can do this — and it does so by re-prompting with both raw results.

## 2. Goals

| # | Goal | Acceptance |
|---|---|---|
| G1 | Subagents pass typed data, not just raw tool results | `$alpha.input.fundamentals` symbol resolves to a typed object |
| G2 | Subagents invoke sub-plans | `trading_agents` exposes internal phases as observable graph nodes |
| G3 | Subagents talk to each other via `consult_subagent` | New tool, runs as an LLM call against the named agent's system prompt |
| G4 | Conditional + loop edges | Graph nodes can branch on output; loop edges bounded by `max_hops` |
| G5 | Bounded LLM cost | Config `multi_subagent.llm_budget_per_turn` caps total subagent-LLM calls |
| G6 | Backwards compatible | Existing `tool == router call` path still works; new runtime is opt-in per intent |

## 3. Non-Goals

- Replacing the LLM Router. Router still produces an intent + plan; the
  plan gets compiled into a graph.
- Replacing the synthesizer. Synthesizer still consumes graph output.
- Distributed execution. Single-process only (asyncio).
- Cross-session state. Each turn has its own GraphState.

## 4. Architecture

### 4.1 Components

```
                        ┌─────────────────────┐
       user message ──▶ │  LLM Router (v2)    │ ── emits RouterPlan
                        └──────────┬──────────┘
                                   │
                                   ▼
                        ┌─────────────────────┐
                        │  Plan → Graph       │ ── compiles to GraphSpec
                        │  Compiler           │    (nodes, edges, refs)
                        └──────────┬──────────┘
                                   │
                                   ▼
   ┌───────────────────────────────────────────────────────┐
   │                GraphExecutor                           │
   │                                                       │
   │  ┌──────────────────────────────────────────────┐     │
   │  │  GraphState  (typed scratchpad)              │     │
   │  │  - intent, symbols, plan_id, run_id          │     │
   │  │  - agent_outputs: dict[id, TypedResult]      │     │
   │  │  - llm_budget_used, hops_remaining           │     │
   │  └──────────────────────────────────────────────┘     │
   │                                                       │
   │  ┌────────┐   msg    ┌────────┐   msg    ┌────────┐    │
   │  │ Node A │──────────▶│ Node B │──────────▶│ Node C │    │
   │  └────────┘          └────────┘          └────────┘    │
   │  planner            data_agent          alpha_agent    │
   │                                                       │
   │  Edges: regular (data), conditional (when), loop (cycle)│
   └───────────────────────────────────────────────────────┘
                                   │
                                   ▼
                        ┌─────────────────────┐
                        │  Synthesizer        │ ── consumes GraphState
                        └─────────────────────┘
```

### 4.2 Node Contract

Every node is `BaseNode` with:

```python
class BaseNode(Protocol):
    id: str                          # unique within graph
    agent_id: str                    # which subagent's system_prompt / tools
    inputs: list[FieldRef]           # declared input fields
    outputs: list[FieldRef]          # declared output fields
    kind: Literal["tool", "llm", "subplan", "consult"]
    async def run(self, state: GraphState, inbox: list[Message]) -> list[Message]:
        ...
```

Four node kinds:
- **tool**: invoke a tool via ToolPipeline (existing behaviour)
- **llm**: call LLM with system_prompt + inbox messages, produce output
- **subplan**: invoke another GraphSpec, merge its output into parent
- **consult**: ask another agent's LLM (without invoking its tools)

### 4.3 Message Format

```python
@dataclass
class Message:
    sender: str          # node id
    receiver: str        # node id or "*" broadcast
    kind: str            # "data" | "question" | "answer" | "delta"
    payload: TypedResult # dataclass / Pydantic, not raw dict
    ts: float            # monotonic
    hop: int             # routing distance from origin
```

`TypedResult` wraps a value with a schema:

```python
@dataclass
class TypedResult:
    schema: type         # Pydantic / dataclass class
    data: Any            # instance
    meta: dict           # source tool, elapsed_ms, confidence
```

This is what unlocks data flow: every message carries a typed payload, so
downstream nodes can `state.agent_outputs["data.fundamentals"].data.pe_ratio`
without parsing strings.

### 4.4 Edges

```python
@dataclass
class Edge:
    src: str            # source node id
    dst: str            # destination node id
    kind: Literal["data", "when", "loop"]
    # For "when": predicate(state, message) -> bool
    # For "loop": max_hops
    field_ref: str | None  # which output field to forward
```

- **data edge**: always fires when src produces output
- **when edge**: fires only when predicate is true
- **loop edge**: re-executes upstream node, capped by max_hops
  (default 3). Each re-execution increments `hop` index on the message;
  downstream nodes can branch on hop > 0 to detect stale inputs.

### 4.5 Symbol Resolution ($refs)

The LLM router emits calls with `$ref` args:

```json
{
  "tool": "compute_alpha_factors",
  "args": {
    "symbol": "$data.quote.symbol",
    "price_window": "$data.fundamentals.last_price * 1.5"
  }
}
```

Resolver rules:
- `$<agent>.<field>` — read from `state.agent_outputs[agent].data.<field>`
- `<expr> * <num>` — arithmetic over resolved values
- `<expr> ? <a> : <b>` — ternary
- Literal fallback if any path fails → the resolved value, not error
- Per-agent scoping: a node can only `$ref` outputs from agents it has
  an explicit edge from. Cross-agent `$ref` without edge = compile error.

Resolver is **pure** — runs once per call before tool dispatch. Failures
fall back to the literal arg the LLM provided (graceful degradation).

### 4.6 Plan → Graph Compilation

RouterPlan → GraphSpec compiler (`PlanCompiler`):

1. **Map calls to nodes** by `_TOOL_TO_AGENT[tool]`. Each call becomes a
   `tool` node owned by its agent.
2. **Derive edges**:
   - Same `parallel_group` → all data edges to a `join` node
   - Cross-group `depends_on` → data edge from upstream group to downstream
   - LLM-emitted `$ref` in args → data edge from referenced agent's output
3. **Add orchestration nodes**:
   - `validate_inputs` (sync, pre-execute)
   - `verify_outputs` (L1, post-execute)
   - `aggregate_signals` (join node, marks L2)
4. **Detect loops**: if an agent's output feeds back to a prior agent's
   input → loop edge with `max_hops=2`.

Output is a `GraphSpec` (Pydantic). Stable serializable form, can be
cached by `(intent, agent_set, plan_hash)`.

### 4.7 GraphExecutor

Replaces `PTCExecutor`. Algorithm:

```
state = GraphState(seed)
queue = PriorityQueue()      # (priority, message)
enqueue all source nodes' empty messages

while queue and state.hops < max_hops and state.llm_used < budget:
    msg = dequeue()
    node = graph[msg.receiver]
    if node not activated: continue

    # Collect inbox: all messages addressed to this node not yet consumed
    inbox = state.consume_inbox(node.id)

    # Run the node
    out_messages = await node.run(state, inbox)

    # Fan out via edges
    for m in out_messages:
        for edge in graph.edges_from(node.id):
            if edge.kind == "data" or (edge.kind == "when" and edge.predicate(state, m)):
                queue.push((priority, Message(receiver=edge.dst, payload=m.payload)))
                if edge.kind == "loop" and state.hops_for(edge) < edge.max_hops:
                    state.hops_for(edge) += 1
                    queue.push((priority, Message(receiver=edge.src, payload=m.payload)))
    state.hops += 1
```

Priority queue implements:
- LLM nodes get priority 1 (run early)
- Tool nodes get priority 2
- Verifier nodes get priority 0 (run as soon as their input is ready)

Activation rule: a node is "activated" once all its `inputs` FieldRefs
have been satisfied (i.e., every `$ref` in its declared inputs points
to a non-empty `state.agent_outputs`).

### 4.8 Backwards Compatibility

- PTC programs still execute via the old `PTCExecutor`. No breaking change.
- GraphSpec is a **superset** that wraps PTC for simple cases:
  - 1 node, no $refs, no conditional edges → compiles to a 1-group PTC
  - The compiler always emits a GraphSpec; GraphExecutor checks if it can
    flatten to PTC for speed
- ToolPipeline unchanged. All existing tools, gates, HITL continue to work.
- Frontend unchanged for the simple case. New SSE events are additive:
  - `agent_message` — payload of a node's output
  - `agent_handoff` — edge fired
  - `graph_state_delta` — top-level state changed

### 4.9 LLM Cost Control

```yaml
multi_subagent:
  llm_budget_per_turn: 5           # max LLM nodes per turn
  max_hops: 8                      # safety cap on graph depth
  consultation_rate_limit: 0.5     # default cap; configurable 0.0–0.8
```

When budget is hit, remaining LLM nodes are skipped with a logged warning
and the graph falls through with what it has. The synthesizer handles
missing data gracefully (already does for tool errors).

### 4.10 Inter-Agent Dialogue: `consult_subagent`

A new built-in tool:

```python
class ConsultSubagentArgs(BaseModel):
    target_agent: str            # one of registered agent ids
    question: str                # what to ask
    context_refs: list[str]      # $refs to include in the LLM context
    max_tokens: int = 512        # response budget
```

Implementation:
1. Build context dict from `context_refs` (resolved via symbol table)
2. Call `target_agent`'s LLM with: system_prompt + context + question
3. Return `str` answer (no tools available in consult mode)
4. Cost: 1 LLM call, counted against `llm_budget_per_turn`

Disallowed:
- `consult_subagent` cannot call `consult_subagent` (no recursive)
- `consult_subagent` cannot invoke tools (read-only)
- `consult_subagent` responses are stored in `state.agent_outputs[id]`
  so the calling node can `$ref` them in subsequent steps

### 4.11 Sub-Plan Nesting: `subplan` node

A `subplan` node wraps another `GraphSpec`. When executed:

```
subplan_node.run(state, inbox):
    sub_state = state.fork()
    sub_graph = self.sub_graph
    sub_result = await GraphExecutor(sub_graph).run_until_done(sub_state)
    return [Message(payload=sub_result.aggregate())]
```

Merging: sub_state's `agent_outputs` are merged into parent state with
a `subplan_<id>_*` prefix to avoid collisions. The parent state sees the
aggregate result.

This makes `trading_agents` a first-class sub-graph instead of an
opaque tool call. Each internal phase (analyst → research → trader → risk)
becomes an observable node.

## 5. File Map

| New file | Lines | Purpose |
|---|---|---|
| `tradingagents/agent_harness/runtime/graph.py` | ~250 | `BaseNode`, `NodeKind`, `GraphSpec`, `Edge` |
| `tradingagents/agent_harness/runtime/state.py` | ~150 | `GraphState`, `TypedResult`, `Message` |
| `tradingagents/agent_harness/runtime/executor.py` | ~350 | `GraphExecutor`, topological walker, loop guard |
| `tradingagents/agent_harness/runtime/resolver.py` | ~150 | `$ref` parser + evaluator |
| `tradingagents/agent_harness/runtime/nodes.py` | ~300 | `ToolNode`, `LLMNode`, `SubplanNode`, `ConsultNode` |
| `tradingagents/agent_harness/runtime/compiler.py` | ~200 | `PlanCompiler` (RouterPlan → GraphSpec) |
| `tradingagents/agent_harness/runtime/agents.py` | ~400 | `data_agent` / `alpha_agent` / `news_agent` / `trading_agents` graph fragments (all nestable) |
| `tradingagents/agent_harness/tools/builtin_consult.py` | ~120 | `consult_subagent` tool wrapper |

| Modified file | Change |
|---|---|
| `tradingagents/agent_harness/core/orchestrator.py` | Use `PlanCompiler` after `LLMRouter.route()`; emit new SSE events |
| `tradingagents/agent_harness/core/llm_router.py` | RouterPrompt teaches `$ref` syntax + agent-aware planning |
| `web/static/harness.js` | Handle `agent_message` / `agent_handoff` events; render data flow |
| `web/static/agent.css` | Styles for data-flow arrows |

Total new code: ~1700 lines, of which ~700 are types/tests.

## 6. Migration / Rollout

The runtime ships behind a feature flag:

```yaml
runtime:
  multi_agent: false   # PTC-only until cutover
  multi_agent: true    # §0.4.36 — multi-agent runtime live
```

Phases:
1. **Phase 1** — types + GraphExecutor (no behavior change, all tests pass)
2. **Phase 2** — `consult_subagent` tool exposed; opt-in via router flag
3. **Phase 3** — `$ref` resolver live; LLM router emits plans with refs
4. **Phase 4** — `subplan` nesting live; `trading_agents` (then `data_agent`, `alpha_agent`) become first-class graphs
5. **Phase 5** — cutover: `multi_agent=true` is default; PTC becomes fallback
6. **Phase 6** — mid-flight state persistence via `AgentRuntimeStore`
   (existing `tradingagents/agent_harness/runtime/store.py`). After every
   node output, snapshot `GraphState` keyed by `(run_id, turn_id, node_id)`
   so a process crash can resume from the last completed node.

Each phase is one commit, gated by the same test suite. Rollback = flip flag.

## 7. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| LLM cost explosion via `consult_subagent` loops | `llm_budget_per_turn=5`, `max_hops=8`, no nested consult |
| `$ref` resolver fails on complex expressions | Pure functions, lazy fallback to LLM's literal arg |
| Subplan nesting deepens failure debugging | All messages logged to `state.message_log`; SSE emits each |
| Existing tests break | Phased rollout; PTC path stays live; flag-gated |
| Agent outputs become stale across hops | Each `TypedResult` carries `meta.source_ts`; verifier can reject stale |
| LLM router emits plans that don't compile | Compiler returns `CompileError`; orchestrator falls back to PTC |
| UI doesn't show what's happening | New SSE events + dock data-flow rows; UI matches backend state |

## 8. Locked Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Per-agent state scoping | **A — declared inputs only** | Safety; cross-agent access goes through `$ref` + explicit edge |
| 2 | Loop edge semantics | **A — re-execute upstream node** | Fresh data; downstream gets `hop` index for staleness tracking |
| 3 | `consult_subagent` rate limit | **B — configurable 0.0–0.8** | Different query types need different caps; default 0.5 |
| 4 | Subplan scope | **B — all agents can nest** | `data_agent` / `alpha_agent` / `trading_agents` all expose graph fragments |
| 5 | Persisted graph state | **B — mid-flight state persisted** | Crash recovery from any node; uses existing checkpoint store |

## 9. Success Metrics

- **Coverage**: 90% of real user queries produce a non-trivial graph
  (>2 nodes, >1 edge) when `multi_agent=true`
- **Cost**: median LLM-call count per turn stays ≤ current + 2
- **Latency**: P95 turn latency ≤ current × 1.5 (with budget bound)
- **Quality**: L3 grounded rate ≥ current baseline (no regression)
- **Observability**: every node activation + edge fire visible in UI
