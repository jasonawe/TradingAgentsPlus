# Phase 4-6 — SubplanNode / Cutover / Persistence Implementation Plan (rev. 1)

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development.

**Goal:** Land Phase 4 (SubplanNode real + orchestration nodes + verifier), Phase 5 (cutover + UI SSE), Phase 6 (mid-flight persistence).

**Spec:** `docs/superpowers/specs/2026-09-24-multi-agent-message-passing-runtime.md`

## Phase 4 (5 work units) — subplan + orchestration

### WU1: `SubplanNode.run` real + `GraphState.fork()`
- `SubplanNode.run` executes `self.sub_graph` in a **forked state** (parent state preserved)
- `GraphState.fork()` returns a **shallow copy with deep-copied mutable state** (agent_outputs, message_log, edge_hops, inbox)
- After sub-graph completes, collect results into `state.agent_outputs[self.id]`
- Sub-graph uses parent's settings + executor constructor
- Test cases:
  - fork() returns independent state (mutating fork doesn't affect parent)
  - SubplanNode happy path: parent state unchanged, agent_outputs populated
  - Nested subplan (Subplan inside Subplan): max_depth guard

### WU2: `symbols` + `plan_id` propagation into GraphState
- Add `symbols: list[str]` and `plan_id: str` fields to `GraphState`
- `PlanCompiler.compile(plan, *, symbols, plan_id)` propagates into GraphSpec metadata
- Orchestrator (`_maybe_run_multi_agent`) reads symbols from RouterPlan
- Test cases:
  - default empty / overridden via compile()
  - symbols propagate to GraphState via _build_graph_state
  - plan_id included in message_log entries

### WU3: orchestration nodes (`validate_inputs` / `verify_outputs` / `aggregate_signals`) in compiler
- `PlanCompiler` auto-emits `validate_inputs` (sync, pre-execute) + `aggregate_signals` (join, marks L2) per spec §4.6 step 3
- `verify_outputs` (L1, post-execute, reads `meta.source_ts` for staleness) — Phase 4 WU4
- These are real node types added to the NodeKind enum
- Test cases:
  - validate_inputs emitted before each entry
  - aggregate_signals joins parallel group branches

### WU4: verifier reads `TypedResult.meta.source_ts`
- New node type `VerifyNode(NodeKind.VERIFY)` checks `meta.source_ts` against `state.now()` for staleness threshold (configurable, default 60s)
- Stale output → produce TypedResult(ok=False, error="stale: source_ts=..."); otherwise pass through
- Wired as `verify_outputs` orchestration node from WU3
- Test cases:
  - fresh output passes through unchanged
  - stale output produces failure marker
  - verify_outputs auto-inserted per spec §4.6 step 3

### WU5: `trading_agents` graph fragment + Phase 4 version bump
- New module `tradingagents/agent_harness/runtime/multi_agent/graph_fragments/trading_agents.py`
- Defines a 3-node subgraph: `data_agent → alpha_agent → synthesizer` (representative multi-step)
- Demo-only: not auto-included but importable for tests
- Version bump v0.7.0 → v0.8.0
- Tag `phase4-subplan-orchestration`

## Phase 5 (2 work units) — cutover + SSE

### WU6: cutover — `multi_agent=true` becomes default
- Change `RuntimeSettings.multi_agent` default from `False` → `True`
- Preserve `TRADINGAGENTS_RUNTIME_MULTI_AGENT=false` env var as kill-switch
- Verify: existing tests pass with new default (no regressions)
- One critical regression risk: Phase 1 wiring test set `multi_agent=False` explicitly, now default is True. Tests must remain stable.
- Test cases:
  - `load_settings()` returns multi_agent=True by default
  - env var kill-switch still works
- Version bump v0.8.0 → v0.9.0
- Tag `phase5-cutover`

### WU7: PlanCompiler cache key + UI SSE events for `agent_message` / `agent_handoff`
- (Combined work unit from Phase 5 ratcheted MEDIUM #2 / round-4)
- PlanCompiler cache key from **immutable parts only**: `(intent, frozenset(nodes), plan_source_hash)` — NOT `GraphSpec.to_dict()` (which carries mutable `hops_used`)
- Front-end `web/static/harness.html`: add EventSource listener for SSE events
- Backend: orchestrator SSE emitter — adds `agent_message` (when a message enqueued) + `agent_handoff` (when message routed)
- Test cases:
  - cache key deterministic for equivalent plans
  - SSE event format stable
  - Front-end renders agent_message events

## Phase 6 (1 work unit) — mid-flight persistence

### WU8: `GraphState` snapshots via `AgentRuntimeStore`
- New `AgentRuntimeStore.save_snapshot(run_id, turn_id, state)` / `load_snapshot(...)` methods
- Executor `_invoke` snapshots before each node run (cheap; advisory; per-spec §7 "SSE emits each")
- Snapshot key = `(run_id, turn_id, node_id)` — fine-grained for resume capability
- Test cases:
  - snapshot saved before node run
  - load_snapshot returns identical state
  - cross-process load works (read-only consumer)
- Version bump v0.9.0 → v1.0.0 (milestone)
- Tag `phase6-persistence`

## Acceptance Checklist (Phase 4 + 5 + 6 total)

- [ ] SubplanNode real implementation + `GraphState.fork()`
- [ ] `symbols` + `plan_id` in GraphState
- [ ] `validate_inputs` / `aggregate_signals` orchestration auto-inserted
- [ ] `verify_outputs` + `source_ts` staleness check
- [ ] `trading_agents` graph fragment importable
- [ ] `multi_agent=true` default; env var kill-switch works
- [ ] PlanCompiler cache key from immutable parts only
- [ ] UI SSE events for `agent_message` / `agent_handoff`
- [ ] `AgentRuntimeStore.save_snapshot` / `load_snapshot`
- [ ] All Phase 1+2+3 217 tests still pass
- [ ] No regression on existing services / pages

## Out of Scope (Future phases)

- Distribute across multiple processes (Phase 6 is single-process)
- Snapshot pruning (Phase 6 keeps everything; operator must prune)
- Resume from arbitrary snapshot (Phase 6 saves but doesn't auto-resume)
