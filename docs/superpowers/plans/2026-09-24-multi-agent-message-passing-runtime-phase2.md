# Phase 2 — Multi-Agent Message-Passing Runtime Implementation Plan (rev. 1)

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land Phase 2 runtime work — real LLM-backed `LLMNode` and `ConsultNode`, `consult_subagent` tool wrapper, `consultation_rate_limit` enforcement, and `state.edge_hops` migration (replace `Edge.hops_used` mutation).

**Spec:** `docs/superpowers/specs/2026-09-24-multi-agent-message-passing-runtime.md`

**Phase 1 contracts preserved (must NOT regress):**
- Executor reset of every edge at top of `run()`; `state.inbox` propagation; priority enqueue
- `_maybe_run_multi_agent()` returns None on fall-through (never raises)
- 5 wiring tests in `test_multi_agent_orchestrator_wiring.py`
- `state.inbox`/`message_log` schema unchanged

## Phase 2 Scope (5 work units)

### Work unit 1: `consult_subagent` tool wrapper (real LLM call)
- Modify: `tradingagents/agent_harness/tools/builtin_consult.py`
- Test: extend `tests/test_consult_subagent.py`

Contract (spec §4.10): build context from `context_refs`, call `target_agent`'s LLM (system_prompt + context + question), return `str`, no tools, 1 LLM call budget, store in `state.agent_outputs[id]`. Refuse with `ConsultBudgetExceeded` when over `consultation_rate_limit * budget_limit`.

### Work unit 2: register `consult_subagent` + `_TOOL_TO_AGENT`
- Modify: `tradingagents/agent_harness/tools/builtin.py` (register via `@tool_registry.register(...)`)
- Modify: `tradingagents/agent_harness/core/orchestrator.py` (add `consult_subagent` to `_TOOL_TO_AGENT`)

### Work unit 3: real `LLMNode.run` + `ConsultNode.run`
- Modify: `tradingagents/agent_harness/runtime/multi_agent/nodes.py`
- Test: extend `tests/test_multi_agent_nodes.py`

`LLMNode`: build prompt + call harness LLM, count against `state.llm_used`, emit result-message.
`ConsultNode`: build ConsultSubagentArgs, invoke consult_subagent, store answer in `state.agent_outputs`.

### Work unit 4: rate limit + depth guard
- Modify: `tradingagents/agent_harness/runtime/multi_agent/executor.py` (consult depth + rate check)
- Modify: `tradingagents/agent_harness/runtime/multi_agent/state.py` (add `consultation_depth: int = 0`)
- Test: extend `tests/test_multi_agent_executor.py`

GraphState.consultation_depth. Refuse `ConsultDepthExceeded` if depth > MAX. Refuse `ConsultBudgetExceeded` if consultation_used over cap. `RuntimeSettings.consultation_rate_limit > 0.8` → SettingsError.

### Work unit 5: `state.edge_hops` migration
- Modify: `state.py` (add `edge_hops: dict[(str, str), int] = field(default_factory=dict)`)
- Modify: `executor.py` (replace `edge.hops_used += 1` with `state.edge_hops[(src,dst)] += 1`; reset `state.edge_hops = {}` at top of `run()`)
- Modify: `tests/test_multi_agent_executor.py` (update assertions)

Per spec §4.7, per-edge counter lives on state, NOT Edge. `Edge.hops_used` removed.

## Commit order (6 commits)

1. `feat(tools): §0.4.35 phase 2 — consult_subagent real LLM wrapper`
2. `feat(runtime): §0.4.35 phase 2 — register consult_subagent + _TOOL_TO_AGENT`
3. `feat(runtime): §0.4.35 phase 2 — LLMNode + ConsultNode real run()`
4. `feat(runtime): §0.4.35 phase 2 — consultation_rate_limit + nested-consult depth guard`
5. `feat(runtime): §0.4.35 phase 2 — state.edge_hops migration`
6. `chore(release): §0.4.35 phase 2 — bump to v0.6.0`

## Acceptance Checklist

- [ ] `consult_subagent` returns non-empty string happy path
- [ ] `consult_subagent` raises ConsultBudgetExceeded / ConsultDepthExceeded
- [ ] `LLMNode.run` happy path returns 1 message, `state.llm_used += 1`
- [ ] `ConsultNode.run` stores answer in `state.agent_outputs[self.id]`
- [ ] `consult_subagent` in `ToolRegistry.list_all()`
- [ ] `_TOOL_TO_AGENT["consult_subagent"]` returns synthesizer
- [ ] `state.edge_hops[(src,dst)]` increments on loop fire
- [ ] All Phase 1 58 tests still pass (regression)

## Out of Scope (Phase 3+)

- Cross-agent `$ref` resolution of consult results (Phase 3)
- SSE events for `agent_message` / `agent_handoff` (Phase 5)
