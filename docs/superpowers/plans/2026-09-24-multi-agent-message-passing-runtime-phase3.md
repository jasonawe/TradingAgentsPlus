# Phase 3 — Cross-Agent `$ref` Resolution Implementation Plan (rev. 1)

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the cross-agent `$ref` resolution chain — `PlanCompiler` scans `call.args` for `$ref`, emits data edges + loop edges, enforces per-agent scoping; `GraphExecutor` writes `state.agent_outputs[node.id]` after each run; activation rule swaps from `has-been-run` to FieldRef-satisfaction per spec §4.7.

**Spec:** `docs/superpowers/specs/2026-09-24-multi-agent-message-passing-runtime.md` (§4.5 / §4.6 / §4.7)

## Phase 3 Scope (4 work units)

### Work unit 1: `state.agent_outputs[node.id] = result` after every node run

**Files:**
- Modify: `tradingagents/agent_harness/runtime/multi_agent/executor.py` (write to state after _invoke)
- Test: extend `tests/test_multi_agent_executor.py`

**Contract:**
- Every node (ToolNode / LLMNode / SubplanNode / ConsultNode) writes its result-data to `state.agent_outputs[node.id]` upon successful completion
- Failed executions also write a placeholder (e.g., `None` or error TypedResult) so subsequent `$ref` calls can detect failure
- Implementation: in `GraphExecutor._invoke`, after `await node.run(state, inbox)`, capture `out_messages[-1].payload.data` and assign to `state.agent_outputs[node.id]`

**Test cases:**
- After ToolNode completes, `state.agent_outputs["a"]` exists
- After LLMNode completes, `state.agent_outputs["a"]` exists
- After failed ConsultNode, `state.agent_outputs["a"]` is `None` (or has error marker)

### Work unit 2: `PlanCompiler` scans `call.args` for `$ref`

**Files:**
- Modify: `tradingagents/agent_harness/runtime/multi_agent/compiler.py`
- Test: extend `tests/test_multi_agent_compiler.py`

**Contract (spec §4.5 / §4.6 step 2):**
- Walk each `call.args` dict; if value is a string starting with `$agent.field`, parse via existing `parse_ref` (Phase 1 resolver)
- Track `referenced_agents` set per call
- After building node + group-order edges, scan `referenced_agents`:
  - **Same group + referenced agent** → fine (no extra edge needed; sequencing handled by group ordering)
  - **Cross-group + referenced agent upstream** → emit data edge from referenced node to this node
  - **Cross-group + referenced agent downstream (or out-of-tree)** → `CompileError` (per-agent scoping, spec §4.5)
  - **Feedback: referenced agent's group is numerically < this call's group** → emit loop edge with `max_hops=2` instead of data edge (spec §4.6 step 4)
  - **Field references to current call's own outputs** → `CompileError` (cannot ref yourself)

**Test cases:**
- `$data.quote.symbol` references data_agent's quote → emits data edge from data_agent to current node
- `$data.quote.symbol` to a node earlier in same plan → emits loop edge with max_hops=2
- `$unknown.x.field` to agent not in plan → `CompileError`
- `$self.x` (ref to current node) → `CompileError`

### Work unit 3: Activator swap (FieldRef-satisfaction, spec §4.7)

**Files:**
- Modify: `tradingagents/agent_harness/runtime/multi_agent/executor.py`
- Test: extend `tests/test_multi_agent_executor.py`

**Contract:**
- Current Phase 2 executor uses `activated: set[str]` with `has-been-run` semantic + `has_loop` bypass
- Phase 3: replace with FieldRef-satisfaction check per spec §4.7:
  - Node with `$ref` inputs → activated when every `$ref` resolves to non-empty `state.agent_outputs[ref.agent]`
  - Node with NO `$ref` inputs (uses raw_args like Phase 1 ToolNodes) → activated UNCONDITIONALLY on message arrival
  - `has_loop` bypass dropped in BOTH cases
  - Loop edges still re-fire via `state.edge_hops`

**Test cases:**
- Node A has input `$data.x`; node B has input `$news.y`; both activate only when upstream ran
- Node A's input is `raw_args` only (no $ref); activates unconditionally on first message
- Loop edge still re-fires within max_hops, even after the regular activation swap

### Work unit 4: `ToolNode.run` resolves `$ref` args at runtime

**Files:**
- Modify: `tradingagents/agent_harness/runtime/multi_agent/nodes.py` (ToolNode.run)
- Test: extend `tests/test_multi_agent_nodes.py`

**Contract:**
- Before invoking tool via ToolPipeline, walk `self.raw_args` and resolve any `$ref` strings via `resolve_ref` (Phase 1)
- Replace `$ref` strings with actual values from `state.agent_outputs`
- Falls back to literal if resolution fails (spec §4.5 graceful degradation)

**Test cases:**
- `$data.quote.symbol` resolves to actual quote value
- `$data.x * 1.5` resolves to arithmetic
- unresolved `$unknown.y` falls back to literal string `"$unknown.y"`
- mixed args: `{"symbol": "$data.quote.symbol", "limit": 10}` → both passed correctly

## Commit order (5 commits)

1. **Work unit 1**: `feat(runtime): §0.4.35 phase 3 — executor writes state.agent_outputs after each run`
2. **Work unit 2**: `feat(runtime): §0.4.35 phase 3 — PlanCompiler scans args for $ref (data/loop/scoping)`
3. **Work unit 3**: `feat(runtime): §0.4.35 phase 3 — FieldRef-satisfaction activation rule`
4. **Work unit 4**: `feat(runtime): §0.4.35 phase 3 — ToolNode resolves $ref args at runtime`
5. **Version bump**: `chore(release): §0.4.35 phase 3 — bump to v0.7.0 ($ref resolution chain)`

## Acceptance Checklist

- [ ] All Phase 1 58 tests still pass (regression)
- [ ] All Phase 2 21 tests still pass (regression)
- [ ] `state.agent_outputs[node.id]` populated for every node run
- [ ] `PlanCompiler` raises `CompileError` for invalid `$ref` (per-agent scoping)
- [ ] `PlanCompiler` emits loop edge for `$ref` to upstream group
- [ ] FieldRef-satisfaction activation replaces `has-been-run` set
- [ ] `ToolNode.run` resolves `$ref` args correctly before tool dispatch
- [ ] Total Phase 1+2+3 test count > 191 (Phase 3 adds ~20 net new tests)

## Out of Scope (Phase 4+)

- LLM router teaches `$ref` syntax (LLM-side training; not runtime)
- `paren` support in resolver (only if Phase 3 surfaces need)
- SSE events for `agent_message` / `agent_handoff` (Phase 5)
- Verify outputs (verifier reads `TypedResult.meta.source_ts`; Phase 4)
- SubplanNode real implementation (Phase 4)
- Trading agents graph fragment (Phase 4)
- `GraphState.fork()` (Phase 4)
