# Supervised AgentRuntime Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Tier 2/3 monolithic Orchestrator path with a persistent in-process AgentRuntime where real agents collaborate through a supervised typed message bus, while preserving Tier 1 speed and the public HTTP/SSE contracts.

**Architecture:** Keep `Harness.orchestrator` as a compatibility-facing TurnCoordinator, but move routing, commands, tool/LLM execution, task scheduling, persistence, verification, synthesis, and projection into focused services. Tier 2/3 analysis runs use a fixed Planner → domain fan-out → evidence verifier → synthesizer → answer verifier backbone with bounded repair/handoff children; write commands use a deterministic `SYSTEM_COMMAND` run.

**Tech Stack:** Python 3.10+, asyncio, Pydantic v2, SQLite, FastAPI/SSE, pytest, existing ToolRegistry/LLMFactory/MemoryManager.

**Reference spec:** `docs/superpowers/specs/2026-09-20-agent-runtime-message-bus-design.md`

---

## Execution Prerequisites

- Execute in a dedicated worktree using `superpowers:subagent-driven-development`.
- The current checkout has user-owned uncommitted changes in overlapping files including `core/orchestrator.py`, `web/app.py`, `web/static/harness.js`, and workflow-spec files. Do not implement from a clean HEAD that silently omits them and do not stage/revert them.
- Before Task 1, establish an explicit baseline that includes those working-tree changes: use a Codex worktree created from the working-tree state, or have the user commit the overlapping work first. Record `git status --short` and the baseline commit in the execution log.
- Every task commit must stage only the exact files listed in that task. If an overlapping file changes unexpectedly, stop and reconcile with the user-owned version before continuing.

---

## File Structure

### New runtime package

- `tradingagents/agent_harness/runtime/models.py`: all normative enums and Pydantic contracts; no I/O.
- `tradingagents/agent_harness/runtime/ingest.py`: recursive hidden-reasoning/secret validation and canonical JSON hashing.
- `tradingagents/agent_harness/runtime/store.py`: thin transaction/facade API over focused persistence repositories.
- `tradingagents/agent_harness/runtime/persistence/db.py`: SQLite connection policy and packaged migrations.
- `tradingagents/agent_harness/runtime/persistence/runs.py`: run state, aggregation metadata, legacy replacement CAS.
- `tradingagents/agent_harness/runtime/persistence/tasks.py`: tasks, dependencies, waits, graph revision/CAS.
- `tradingagents/agent_harness/runtime/persistence/events.py`: messages, artifacts, runtime event sequence, outbox rows.
- `tradingagents/agent_harness/runtime/persistence/operations.py`: tool operations and approvals.
- `tradingagents/agent_harness/runtime/persistence/usage.py`: token reservations and settlement.
- `tradingagents/agent_harness/runtime/outbox.py`: claim/deliver/retry/dead-letter worker.
- `tradingagents/agent_harness/runtime/policy.py`: budgets, cycles, handoff selection, GraphPatch authorization.
- `tradingagents/agent_harness/runtime/scheduler.py`: dependency readiness, task claim, wait resolution, run aggregation.
- `tradingagents/agent_harness/runtime/dispatcher.py`: AgentRegistry and CommandExecutor dispatch only.
- `tradingagents/agent_harness/runtime/projector.py`: durable event → compatible SSE and inspector projections.
- `tradingagents/agent_harness/runtime/runtime.py`: public start/resume/cancel/answer/confirm/reconcile facade.
- `tradingagents/agent_harness/runtime/migrations/001_initial.sql`: versioned Runtime schema.
- `tradingagents/agent_harness/runtime/__init__.py`: stable public exports.

### New core services

- `tradingagents/agent_harness/core/router.py`: `RouteDecision` construction from existing tier classifiers.
- `tradingagents/agent_harness/core/command_resolver.py`: entity/op → typed CommandSpec.
- `tradingagents/agent_harness/core/tool_executor.py`: governed tool execution and operation/HITL state.
- `tradingagents/agent_harness/core/llm_executor.py`: governed LLM calls and persisted usage reservations.
- `tradingagents/agent_harness/core/context_assembler.py`: dependency evidence plus L1/L2/L3 context, including pending projection overlay.
- `tradingagents/agent_harness/core/turn_repository.py`: session accounting and idempotent memory projection.
- `tradingagents/agent_harness/core/turn_coordinator.py`: thin Tier 1 vs Runtime coordinator.
- `web/harness_runtime_api.py`: framework-light request/SSE adapter functions mounted only at final cutover.

### Existing files replaced or migrated

- `tradingagents/agent_harness/core/orchestrator.py`: replace implementation with compatibility re-exports for TurnCoordinator.
- `tradingagents/agent_harness/agents/base.py`: V2 `AgentTask → AgentReply` contract.
- `tradingagents/agent_harness/agents/{planner,data_agent,news_agent,alpha_agent,verifier,synthesizer}.py`: real Runtime participants.
- `tradingagents/agent_harness/agents/{registry,subagent_provider}.py`: descriptors, V1 adapter, governed proxies.
- `tradingagents/agent_harness/harness.py`: assemble new services and expose existing setters through facades.
- `tradingagents/agent_harness/memory/l1_session.py`: atomic projection receipts.
- `tradingagents/agent_harness/core/workflow_spec_registry.py`: bind `WorkflowServices`, not Orchestrator internals.
- `web/app.py`: chat/resume/poll/confirm/reconcile/trace adapters.
- `web/static/harness.js`: waiting-user, repair, handoff, run cursor, inspector rendering.
- `pyproject.toml`: package Runtime SQL migrations.

### New focused tests

- `tests/test_agent_runtime_models.py`
- `tests/test_agent_message_ingestor.py`
- `tests/test_agent_runtime_store.py`
- `tests/test_agent_runtime_outbox.py`
- `tests/test_agent_runtime_policy.py`
- `tests/test_agent_runtime_scheduler.py`
- `tests/test_agent_runtime_dispatcher.py`
- `tests/test_tool_executor.py`
- `tests/test_llm_executor.py`
- `tests/test_runtime_agents.py`
- `tests/test_turn_coordinator.py`
- `tests/test_agent_runtime_recovery.py`
- `tests/test_agent_runtime_web_contract.py`
- `tests/test_agent_runtime_architecture.py`

---

## Chunk 1: Protocol, Ingestion, and Durable Store

### Task 1: Define the V2 runtime contracts

**Files:**
- Create: `tradingagents/agent_harness/runtime/__init__.py`
- Create: `tradingagents/agent_harness/runtime/models.py`
- Test: `tests/test_agent_runtime_models.py`

- [ ] **Step 1: Write failing schema tests**

Create tests for: `RouteDecision`; globally generated task IDs vs planner-local `task_key`; `AgentMessage.type == payload.kind`; `execution_attempt`; `PlanGraph`; `AgentReply`; `GraphPatch`; command/system task kinds; task/run/operation/outbox states; and every discriminated payload. Include rejection cases for extra fields, an invalid confidence, an invalid dependency mode, and mismatched message type/payload.

```python
def test_agent_message_rejects_kind_mismatch():
    with pytest.raises(ValidationError):
        AgentMessage(
            message_id="m1", seq=1, run_id="r1", turn_id="t1",
            task_id="task1", parent_task_id=None,
            sender="news_agent", recipient="runtime",
            type=AgentMessageType.RESULT,
            payload=ProgressPayload(kind="PROGRESS", stage="fetch", summary="ok"),
            evidence_refs=[], causation_id=None, correlation_id="c1",
            idempotency_key="i1", execution_attempt=1,
            created_at=datetime.now(timezone.utc),
        )
```

- [ ] **Step 2: Run the model tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_models.py`

Expected: FAIL during import because `tradingagents.agent_harness.runtime.models` does not exist.

- [ ] **Step 3: Implement strict enums and Pydantic models**

Use `ConfigDict(extra="forbid", frozen=True)` on immutable envelopes. Define `AgentMessageType`, `TaskState`, `RunState`, `RunKind`, `TaskKind`, `OperationState`, `OutboxState`, `DependencyMode`, `EvidenceRef`, `VerifiedEvidenceRef`, every payload from spec §17.3, `AgentTask`, `AgentReply`, `PlanTask`, `PlanGraph`, `RouteDecision`, `CommandSpec`, and GraphPatch operations. Add an `@model_validator(mode="after")` to enforce `message.type.value == message.payload.kind`.

Keep models under 500 lines by grouping only data contracts here; no SQLite or scheduling helpers.

- [ ] **Step 4: Run focused tests and lint**

Run: `pytest -q tests/test_agent_runtime_models.py`

Run: `ruff check tradingagents/agent_harness/runtime/models.py tests/test_agent_runtime_models.py`

Expected: PASS; no lint findings.

- [ ] **Step 5: Commit the protocol**

```bash
git add tradingagents/agent_harness/runtime/__init__.py tradingagents/agent_harness/runtime/models.py tests/test_agent_runtime_models.py
git commit -m "feat(harness): define agent runtime protocol"
```

### Task 2: Enforce ingestion and canonical identity

**Files:**
- Create: `tradingagents/agent_harness/runtime/ingest.py`
- Test: `tests/test_agent_message_ingestor.py`

- [ ] **Step 1: Write failing recursive-ingestion tests**

Cover nested dict/list traversal, depth 12, node limit 10,000, case-insensitive hidden-reasoning keys, secret redaction, Tool-schema sensitive fields, forbidden `<scratchpad>` markers, stable canonical JSON, payload SHA-256, UUIDv5 message idempotency, and rejection before any store call.

```python
@pytest.mark.parametrize("key", ["reasoning", "Chain_Of_Thought", "scratchpad"])
def test_ingestor_rejects_hidden_reasoning_at_any_depth(key):
    with pytest.raises(UnsafeMessageError):
        MessageIngestor().sanitize({"outer": [{key: "private"}]})


def test_ingestor_redacts_nested_secret():
    assert MessageIngestor().sanitize({"cfg": {"api_key": "abc"}}) == {
        "cfg": {"api_key": {"kind": "REDACTED"}}
    }
```

- [ ] **Step 2: Run the tests to verify RED**

Run: `pytest -q tests/test_agent_message_ingestor.py`

Expected: FAIL because `MessageIngestor` is undefined.

- [ ] **Step 3: Implement `MessageIngestor`**

Implement iterative recursive traversal so depth/node limits are explicit. Reject hidden-reasoning keys/markers, replace secret values with `{"kind": "REDACTED"}`, then validate the typed model. Implement `canonical_json()`, `payload_sha256()`, and `message_idempotency_key()` using UUIDv5 over `task_id|execution_attempt|type|recipient|causation_id|hash`.

- [ ] **Step 4: Verify focused tests**

Run: `pytest -q tests/test_agent_message_ingestor.py tests/test_agent_runtime_models.py`

Expected: PASS.

- [ ] **Step 5: Commit ingestion**

```bash
git add tradingagents/agent_harness/runtime/ingest.py tests/test_agent_message_ingestor.py
git commit -m "feat(harness): validate agent message ingestion"
```

### Task 3: Add the versioned Runtime SQLite schema

**Files:**
- Create: `tradingagents/agent_harness/runtime/migrations/001_initial.sql`
- Modify: `pyproject.toml` under `[tool.setuptools.package-data]`
- Create: `tradingagents/agent_harness/runtime/store.py`
- Create: `tradingagents/agent_harness/runtime/persistence/__init__.py`
- Create: `tradingagents/agent_harness/runtime/persistence/db.py`
- Create: `tradingagents/agent_harness/runtime/persistence/runs.py`
- Create: `tradingagents/agent_harness/runtime/persistence/tasks.py`
- Create: `tradingagents/agent_harness/runtime/persistence/events.py`
- Create: `tradingagents/agent_harness/runtime/persistence/operations.py`
- Create: `tradingagents/agent_harness/runtime/persistence/usage.py`
- Test: `tests/test_agent_runtime_store.py`

- [ ] **Step 1: Write failing migration/schema tests**

Instantiate `AgentRuntimeStore(tmp_path / "runtime.sqlite")`, then assert every spec table, foreign key, partial active-run index, `(run_id, seq)` uniqueness, `agent_task_waits.failure_policy`, non-null outbox `delivery_key`, operation/version fields, usage-call uniqueness, and legacy migration uniqueness. Also assert applying migrations twice is idempotent.

- [ ] **Step 2: Run migration tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_store.py -k schema`

Expected: FAIL because RuntimeStore/migrations do not exist.

- [ ] **Step 3: Write `001_initial.sql`**

Create every table with these exact column groups and constraints (JSON values are `TEXT NOT NULL` unless marked nullable; timestamps are UTC ISO-8601 `TEXT`):

- `schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)`.
- `agent_runs(run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL, run_kind TEXT NOT NULL, state TEXT NOT NULL, route_json TEXT NOT NULL, budgets_json TEXT NOT NULL, final_result_json TEXT, terminal_reason TEXT, worker_id TEXT, lease_expires_at TEXT, heartbeat_at TEXT, next_seq INTEGER NOT NULL DEFAULT 1, terminal_seq INTEGER, graph_revision INTEGER NOT NULL DEFAULT 0, session_projection_state TEXT NOT NULL DEFAULT 'PENDING', session_projected_at TEXT, version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)` plus the active-session partial unique index for `PLANNING/RUNNING/WAITING_USER/WAITING_APPROVAL/NEEDS_RECONCILIATION`.
- `agent_tasks(task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_task_id TEXT, kind TEXT NOT NULL, agent_name TEXT, system_handler TEXT, capability TEXT, objective TEXT NOT NULL, inputs_json TEXT NOT NULL, required INTEGER NOT NULL, state TEXT NOT NULL, execution_attempt INTEGER NOT NULL DEFAULT 0, max_execution_attempts INTEGER NOT NULL, resume_count INTEGER NOT NULL DEFAULT 0, repair_count INTEGER NOT NULL DEFAULT 0, handoff_depth INTEGER NOT NULL DEFAULT 0, repair_of_task_id TEXT, handoff_from_task_id TEXT, worker_id TEXT, lease_expires_at TEXT, heartbeat_at TEXT, result_json TEXT, error_json TEXT, version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)` with run/parent foreign keys and run/state index.
- `agent_task_dependencies(run_id TEXT NOT NULL, task_id TEXT NOT NULL, depends_on_task_id TEXT NOT NULL, condition TEXT NOT NULL, PRIMARY KEY(run_id, task_id, depends_on_task_id))` with task foreign keys.
- `agent_task_waits(wait_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, waiter_task_id TEXT NOT NULL, child_task_id TEXT NOT NULL, wait_kind TEXT NOT NULL, failure_policy TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT, UNIQUE(waiter_task_id, child_task_id))`.
- `agent_artifacts(artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, producer_task_id TEXT NOT NULL, source_type TEXT NOT NULL, source_name TEXT NOT NULL, content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL, as_of TEXT, created_at TEXT NOT NULL)`.
- `agent_messages(message_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, turn_id TEXT NOT NULL, seq INTEGER NOT NULL, task_id TEXT NOT NULL, parent_task_id TEXT, sender TEXT NOT NULL, recipient TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL, evidence_refs_json TEXT NOT NULL, causation_id TEXT, correlation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, execution_attempt INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id, seq))`.
- `runtime_events(event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL, event_type TEXT NOT NULL, task_id TEXT, message_id TEXT, surface TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id, seq))`.
- `agent_outbox(outbox_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT, message_id TEXT, source_event_id TEXT NOT NULL, delivery_key TEXT NOT NULL, destination TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, claimed_by TEXT, claimed_at TEXT, last_error TEXT, created_at TEXT NOT NULL, delivered_at TEXT, UNIQUE(destination, delivery_key))`.
- `agent_operations(operation_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, task_id TEXT NOT NULL, tool_name TEXT NOT NULL, args_hash TEXT NOT NULL, redacted_args_json TEXT NOT NULL, side_effect_mode TEXT NOT NULL, approval_id TEXT, state TEXT NOT NULL, remote_idempotency_key TEXT, result_json TEXT, error_json TEXT, version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)`.
- `agent_approvals(approval_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, audit_id INTEGER, decision TEXT NOT NULL, decided_by TEXT, expires_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, decided_at TEXT)`.
- `agent_usage_reservations(reservation_id TEXT PRIMARY KEY, call_id TEXT NOT NULL, run_id TEXT NOT NULL, task_id TEXT NOT NULL, agent_name TEXT NOT NULL, execution_attempt INTEGER NOT NULL, call_ordinal INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL, reserved_input_tokens INTEGER NOT NULL, reserved_output_tokens INTEGER NOT NULL, actual_input_tokens INTEGER, actual_output_tokens INTEGER, provider_attempt_count INTEGER NOT NULL, lease_expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(call_id, provider_attempt_count))`.
- `agent_legacy_interruptions(migration_key TEXT PRIMARY KEY, legacy_store_identity TEXT NOT NULL, session_id TEXT NOT NULL, workflow_name TEXT, milestone_id TEXT NOT NULL, node_position TEXT NOT NULL, original_user_message TEXT, state TEXT NOT NULL, replacement_run_id TEXT, imported_at TEXT NOT NULL)`.

Add foreign keys and indexes used by every query named in Tasks 4–6; do not add a generic key/value state table.

- [ ] **Step 4: Implement migration loading and connection policy**

`persistence/db.py` must create the parent directory, enable foreign keys and WAL, load packaged SQL via `importlib.resources`, and execute pending numbered migrations transactionally. Each repository receives the same DB boundary; `AgentRuntimeStore` exposes the composed API without embedding repository SQL. Add `pyproject.toml` package data for `agent_harness/runtime/migrations/*.sql`.

- [ ] **Step 5: Run schema tests**

Run: `pytest -q tests/test_agent_runtime_store.py -k schema`

Expected: PASS.

- [ ] **Step 6: Commit schema/store bootstrap**

```bash
git add pyproject.toml tradingagents/agent_harness/runtime/migrations/001_initial.sql tradingagents/agent_harness/runtime/store.py tradingagents/agent_harness/runtime/persistence tests/test_agent_runtime_store.py
git commit -m "feat(harness): add persistent agent runtime store"
```

### Task 4: Implement atomic messages, task CAS, graph commits, and event sequence

**Files:**
- Modify: `tradingagents/agent_harness/runtime/store.py`
- Modify: `tradingagents/agent_harness/runtime/persistence/runs.py`
- Modify: `tradingagents/agent_harness/runtime/persistence/tasks.py`
- Modify: `tradingagents/agent_harness/runtime/persistence/events.py`
- Test: `tests/test_agent_runtime_store.py`

- [ ] **Step 1: Add failing transactional-store tests**

Test: one active run per session; global UUIDv7 task IDs; planner task-key remapping; strict state/version CAS; atomic AgentMessage + runtime_event + outbox; monotonic run seq under concurrent threads; graph revision CAS; accepted/rejected GraphPatch; wait persistence; wait resolution after restart; run aggregation by run kind; terminal_seq; and deterministic queries for active/latest recoverable runs.

```python
def test_message_event_and_outbox_commit_atomically(store, run, task):
    stored = store.append_message_and_transition(
        message=make_result_message(run, task),
        expected_task_state=TaskState.RUNNING,
        next_task_state=TaskState.SUCCEEDED,
        outbox_destinations=("scheduler", "sse"),
    )
    assert stored.message.seq == stored.event.seq
    assert store.list_outbox(run.run_id)[0].source_event_id == stored.event.event_id
```

- [ ] **Step 2: Run focused tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_store.py -k 'cas or message or graph or sequence or aggregate'`

Expected: FAIL because transaction methods are missing.

- [ ] **Step 3: Implement small transaction methods**

Add explicit methods rather than a generic SQL API. `RunRepository` owns `create_run`, aggregation, terminal marking, active/latest lookup, and legacy replacement. `TaskRepository` owns graph insert, claim, transition, patch, dependency and wait resolution. `EventRepository` owns artifacts, messages, monotonic events, and outbox creation. `AgentRuntimeStore` coordinates the few transactions spanning repositories. Every mutation takes an expected version/state; use `BEGIN IMMEDIATE` only where sequence/graph serialization requires it.

- [ ] **Step 4: Add concurrency and rollback fault tests**

Inject exceptions after message insert but before transition/outbox and assert all rows roll back. Race two task completions and assert only one CAS wins. Race legacy replacement creation and assert one replacement_run_id.

- [ ] **Step 5: Run store tests**

Run: `pytest -q tests/test_agent_runtime_store.py`

Expected: PASS.

- [ ] **Step 6: Commit transactional store**

```bash
git add tradingagents/agent_harness/runtime/store.py tradingagents/agent_harness/runtime/persistence tests/test_agent_runtime_store.py
git commit -m "feat(harness): persist runtime state transitions"
```

### Task 5: Implement outbox delivery and recovery scanning

**Files:**
- Create: `tradingagents/agent_harness/runtime/outbox.py`
- Modify: `tradingagents/agent_harness/runtime/store.py`
- Modify: `tradingagents/agent_harness/runtime/persistence/events.py`
- Test: `tests/test_agent_runtime_outbox.py`
- Test: `tests/test_agent_runtime_recovery.py`

- [ ] **Step 1: Write failing outbox tests**

Cover PENDING → CLAIMED → DELIVERED, 60-second claim expiry, exponential retry capped at 60 seconds, DEAD after 10 attempts, non-null event-derived delivery keys, scheduler DEAD failing the run, audit DEAD producing only health warnings, and duplicate delivery no-op.

- [ ] **Step 2: Write failing recovery-boundary tests**

Inject crashes at: business commit before enqueue; child completion before wait resolution; task completion before run aggregation; and claimed-outbox timeout. Assert restart scanning releases only eligible tasks, resolves waits once, preserves event seq, and never regenerates PROGRESS.

- [ ] **Step 3: Run tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_outbox.py tests/test_agent_runtime_recovery.py`

Expected: FAIL because OutboxWorker and recovery scan are missing.

- [ ] **Step 4: Implement `OutboxWorker` and `recover()`**

Inject destination handlers into OutboxWorker. Use store CAS for claim/ack/nack. Implement bounded `run_once(limit=100)` for deterministic tests and an async loop for Harness startup. Recovery resets expired leases, recomputes dependency readiness, resolves persisted waits, aggregates runs, and enqueues missing scheduler outbox rows idempotently.

- [ ] **Step 5: Verify outbox/recovery tests**

Run: `pytest -q tests/test_agent_runtime_outbox.py tests/test_agent_runtime_recovery.py`

Expected: PASS.

- [ ] **Step 6: Commit outbox/recovery**

```bash
git add tradingagents/agent_harness/runtime/outbox.py tradingagents/agent_harness/runtime/store.py tradingagents/agent_harness/runtime/persistence/events.py tests/test_agent_runtime_outbox.py tests/test_agent_runtime_recovery.py
git commit -m "feat(harness): recover runtime through durable outbox"
```

### Task 6: Add atomic L1 projection receipts

**Files:**
- Modify: `tradingagents/agent_harness/memory/l1_session.py`
- Modify: `tradingagents/agent_harness/memory/manager.py`
- Test: `tests/test_agent_runtime_recovery.py`
- Test: `tests/test_l1_lg_backend_regression.py`

- [ ] **Step 1: Write failing projection-idempotency tests**

Call `append_projected_exchange("s1", "question", "answer", projection_key="runtime:r1")` twice and assert exactly one user/assistant pair plus one receipt. Cover both simple SQLite and LangGraph-backed session storage. Simulate a crash after target append but before Runtime projection DELIVERED, retry, and assert no duplicate messages.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_recovery.py -k projection tests/test_l1_lg_backend_regression.py`

Expected: FAIL because the projection API/receipt table is absent.

- [ ] **Step 3: Implement the projection API**

Add `append_projected_exchange(session_id, user_text, assistant_text, projection_key) -> Literal["applied", "already_applied"]`. In the same target SQLite transaction, insert `runtime_projection_receipts(projection_key PRIMARY KEY, created_at)` and both history messages. For LangGraph session files, create/use the receipt table on the same connection used for checkpoint/message writes. Raise `UnsupportedProjectionBackend` if atomic receipt support is unavailable.

- [ ] **Step 4: Verify memory regressions**

Run: `pytest -q tests/test_agent_runtime_recovery.py -k projection tests/test_l1_lg_backend_regression.py tests/test_d8_context_memory.py`

Expected: PASS.

- [ ] **Step 5: Commit projection receipts**

```bash
git add tradingagents/agent_harness/memory/l1_session.py tradingagents/agent_harness/memory/manager.py tests/test_agent_runtime_recovery.py tests/test_l1_lg_backend_regression.py
git commit -m "feat(harness): make runtime memory projection idempotent"
```

---

## Chunk 2: Governed Executors and AgentRuntime

### Task 7: Extract routing and deterministic command resolution

**Files:**
- Create: `tradingagents/agent_harness/core/router.py`
- Create: `tradingagents/agent_harness/core/command_resolver.py`
- Modify: `tradingagents/agent_harness/core/tier.py`
- Test: `tests/test_turn_coordinator.py`
- Test: `tests/test_crud_dispatch.py`

- [ ] **Step 1: Write failing Router tests**

Cover explicit/carry-forward symbols, slots, every CRUD `(Intent, Op)`, Tier 1 read, SYSTEM_COMMAND write, Tier 2 analysis, Tier 3 deep multi-symbol, and LLM-unavailable degradation. Assert `RouteDecision.route_kind` and that Router performs no tool/LLM call.

- [ ] **Step 2: Write failing CommandResolver tests**

Move the current `_CRUD_DISPATCH` behavior into tests for `CommandResolver.resolve(route, user_message)`. Require typed CommandSpec for notes, alerts, watchlist, scheduled jobs, runs, reports, bulk delete, slot-derived note body/threshold/cron/date, and carry-forward focus. Assert unsupported `(entity, op)` raises `UnsupportedCommand` rather than returning an empty plan.

- [ ] **Step 3: Run focused tests to verify RED**

Run: `pytest -q tests/test_turn_coordinator.py tests/test_crud_dispatch.py`

Expected: FAIL because Router/CommandResolver do not exist.

- [ ] **Step 4: Implement Router and CommandResolver**

Reuse `classify`, `classify_multi`, `extract_slots`, `extract_symbols`, and `maybe_degrade_to_tier1`; do not copy regexes. Move CRUD args helpers out of `orchestrator.py`. The resolver must return CommandSpec only; it cannot invoke tools or approvals.

- [ ] **Step 5: Verify routing tests**

Run: `pytest -q tests/test_turn_coordinator.py tests/test_crud_dispatch.py tests/test_tier_degrade.py tests/test_tier_normalize.py tests/test_step20_slot_carry_forward.py`

Expected: PASS.

- [ ] **Step 6: Commit routing extraction**

```bash
git add tradingagents/agent_harness/core/router.py tradingagents/agent_harness/core/command_resolver.py tradingagents/agent_harness/core/tier.py tests/test_turn_coordinator.py tests/test_crud_dispatch.py
git commit -m "refactor(harness): extract routing and commands"
```

### Task 8: Implement governed ToolExecutor and operation state

**Files:**
- Create: `tradingagents/agent_harness/core/tool_executor.py`
- Modify: `tradingagents/agent_harness/runtime/persistence/operations.py`
- Modify: `tradingagents/agent_harness/tools/schema.py`
- Modify: `tradingagents/agent_harness/tools/builtin.py`
- Modify: `tradingagents/agent_harness/tools/pipeline.py`
- Test: `tests/test_tool_executor.py`
- Test: `tests/test_tool_pipeline.py`

- [ ] **Step 1: Write failing execution tests**

Test allowed/denied AgentScope, Pydantic coercion, pipeline hooks, timeout, retry/circuit breaker, operation idempotency, read dedupe, LOCAL_TRANSACTIONAL, REMOTE_IDEMPOTENT, REMOTE_RECONCILABLE, NON_IDEMPOTENT, approval pause, duplicate confirmation, rejection/expiry, `INDETERMINATE`, and `RETRY_AUTHORIZED → EXECUTING`.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_tool_executor.py`

Expected: FAIL because ToolExecutor and `side_effect_mode` metadata are absent.

- [ ] **Step 3: Add tool side-effect metadata**

Add a `SideEffectMode` enum and require all write tools to declare one in ToolSchema metadata. Mark existing local SQLite CRUD as `LOCAL_TRANSACTIONAL`; fail registry installation when a write tool omits the declaration. Add optional `reconcile_handler` only for REMOTE_RECONCILABLE tools.

- [ ] **Step 4: Implement ToolExecutor**

Expose `execute_read(task, tool_name, raw_args, context)` and `execute_operation(task, CommandSpec, context)`. It must call ToolRegistry only inside this file, enforce scope, persist operation before side effect, use ToolPipeline, and return typed ToolExecutionResult. ASK creates approval/confirm_request and returns WAITING_APPROVAL; no endpoint may call `tool.invoke()` directly.

- [ ] **Step 5: Run tool/HITL regressions**

Run: `pytest -q tests/test_tool_executor.py tests/test_tool_pipeline.py tests/test_step38_hitl_race.py tests/test_double_confirm_race.py tests/test_o10_audit_executed.py`

Expected: PASS.

- [ ] **Step 6: Commit ToolExecutor**

```bash
git add tradingagents/agent_harness/core/tool_executor.py tradingagents/agent_harness/runtime/persistence/operations.py tradingagents/agent_harness/tools/schema.py tradingagents/agent_harness/tools/builtin.py tradingagents/agent_harness/tools/pipeline.py tests/test_tool_executor.py tests/test_tool_pipeline.py
git commit -m "feat(harness): govern tool execution and approvals"
```

### Task 9: Implement LLMExecutor and persistent UsageLedger

**Files:**
- Create: `tradingagents/agent_harness/core/llm_executor.py`
- Modify: `tradingagents/agent_harness/runtime/store.py`
- Modify: `tradingagents/agent_harness/runtime/persistence/usage.py`
- Modify: `tradingagents/agent_harness/llm/factory.py`
- Test: `tests/test_llm_executor.py`
- Test: `tests/test_token_usage.py`

- [ ] **Step 1: Write failing usage-reservation tests**

Cover atomic budget reservation under concurrent calls, multiple call ordinals per task attempt, provider retries with child reservation rows, actual usage settlement, pre-send release, ambiguous timeout charged at reservation ceiling, lease-expiry settlement, cache hits, budget exhaustion before provider call, and `usage_summary.by_agent`.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_llm_executor.py`

Expected: FAIL because LLMExecutor/UsageLedger are undefined.

- [ ] **Step 3: Implement LLMExecutor**

Create `complete(task, agent_name, messages, *, provider=None, model=None, max_tokens=None)` plus a governed sync bridge for V1 agents. Allocate call ordinal and UUIDv5 call_id, reserve through RuntimeStore, invoke LLMFactory provider in a worker thread where needed, apply ResolvedRetryPolicy, and persist usage/result classification before returning `LLMResponse`.

- [ ] **Step 4: Preserve existing cache and identity behavior**

Keep LLMFactory/AppIdentity/LLMResponseCache wiring; LLMExecutor wraps the factory rather than reimplementing provider construction. Add tests proving cache and identity headers still pass through.

- [ ] **Step 5: Run LLM/accounting tests**

Run: `pytest -q tests/test_llm_executor.py tests/test_token_usage.py tests/test_token_usage_r5_provider.py tests/test_llm_cache.py tests/test_app_identity.py tests/test_resolved_retry_policy.py`

Expected: PASS.

- [ ] **Step 6: Commit LLMExecutor**

```bash
git add tradingagents/agent_harness/core/llm_executor.py tradingagents/agent_harness/runtime/store.py tradingagents/agent_harness/runtime/persistence/usage.py tradingagents/agent_harness/llm/factory.py tests/test_llm_executor.py tests/test_token_usage.py
git commit -m "feat(harness): enforce persistent llm budgets"
```

### Task 10: Implement PolicyGuard and bounded GraphPatch

**Files:**
- Create: `tradingagents/agent_harness/runtime/policy.py`
- Modify: `tradingagents/agent_harness/runtime/store.py` (deferred — no change required for tests)
- Test: `tests/test_agent_runtime_policy.py`

- [x] **Step 1: Write failing policy tests**

Test registered capability/scope checks, graph cycles, max messages/tasks/repairs/handoff depth/deadline/tokens, deterministic handoff ordering `(capability, scope, -priority, name)`, ancestor exclusion, raw GraphPatch rejection, stale graph retry once, accepted repair/handoff child, and `on_reject` FAIL vs RESUME.

- [x] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_policy.py`

Expected: FAIL because PolicyGuard is missing.

- [x] **Step 3: Implement PolicyGuard**

Expose pure decisions: `validate_initial_graph`, `select_handoff_agent`, `build_patch_from_message`, and `authorize_patch`. Only accept persisted HANDOFF_REQUEST/REPAIR_REQUEST. Return typed PolicyDecision; RuntimeStore alone applies an approved patch transaction.

- [x] **Step 4: Verify policy/store integration**

Run: `pytest -q tests/test_agent_runtime_policy.py tests/test_agent_runtime_store.py -k graph`

Expected: PASS.

- [x] **Step 5: Commit policy**

```bash
git add tradingagents/agent_harness/runtime/policy.py tests/test_agent_runtime_policy.py
git commit -m "feat(harness): bound dynamic agent repair"
```

Result: 15/15 policy tests GREEN, 214/214 full regression GREEN. Commit `9dd3c25`.

### Task 11: Implement scheduler and run aggregation

**Files:**
- Create: `tradingagents/agent_harness/runtime/scheduler.py`
- Create: `tradingagents/agent_harness/runtime/migrations/002_fix_dep_fk.sql` (FK fix)
- Test: `tests/test_agent_runtime_scheduler.py`
- Test: `tests/test_agent_runtime_recovery.py` (3 new scheduler×recovery tests)

- [x] **Step 1: Write failing scheduler tests**

Cover ON_SUCCESS/ON_TERMINAL readiness, parallel ready claims, required vs optional failure, WAITING_MESSAGE vs WAITING_CHILD, child failure policies, cancel/complete CAS races, AGENT_ANALYSIS aggregation, SYSTEM_COMMAND aggregation, NEEDS_RECONCILIATION, and no duplicate releases after recovery.

- [x] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_scheduler.py`

Expected: FAIL because TaskScheduler is missing.

- [x] **Step 3: Implement TaskScheduler**

Keep it free of Agent/Tool logic. It queries RuntimeStore for ready tasks, claims with lease/version CAS, resolves persisted waits, propagates cancellation, and invokes the spec run-kind aggregation table after every state transition. Provide `run_once()` for deterministic tests and `run_until_blocked(run_id)` for Runtime.

- [x] **Step 4: Run scheduler/recovery tests**

Run: `pytest -q tests/test_agent_runtime_scheduler.py tests/test_agent_runtime_recovery.py`

Expected: PASS.

- [x] **Step 5: Commit scheduler**

Result: 17/17 scheduler tests GREEN, 28/28 scheduler+recovery GREEN, 234/234 full regression GREEN. Commit `8967115`.

### Task 12: Implement dispatcher and AgentRuntime facade

**Files:**
- Create: `tradingagents/agent_harness/runtime/dispatcher.py`
- Create: `tradingagents/agent_harness/runtime/runtime.py`
- Modify: `tradingagents/agent_harness/runtime/__init__.py`
- Test: `tests/test_agent_runtime_dispatcher.py`

- [x] **Step 1: Write failing dispatch/runtime tests**

Assert AGENT tasks call only `AgentRegistry.get(name).run(task, context=context)`; SYSTEM_COMMAND calls only CommandResolver; outgoing messages pass MessageIngestor before store; AgentReply creates exactly one RESULT; progress persists before projection; start/resume/answer/confirm/cancel/reconcile obey state and correlation; session has one active run; and legacy replacement CAS reuses the same run.

- [x] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_dispatcher.py tests/test_agent_runtime_recovery.py`

Expected: FAIL because Dispatcher/AgentRuntime are missing.

- [x] **Step 3: Implement AgentDispatcher**

Define a narrow `ContextProvider` Protocol (`assemble(task) -> AgentContextBundle`) and inject a deterministic fake in this chunk's tests; Chunk 3's ContextAssembler implements it. Also inject AgentRegistry, CommandResolver, MessageIngestor, ToolExecutor, and LLMExecutor. Dispatcher builds AgentExecutionContext, awaits V2 agent, validates AgentReply, and commits reply/outgoing/state atomically. It never chooses graph topology.

- [x] **Step 4: Implement AgentRuntime public methods**

Implement `start_analysis`, `start_command`, `stream`, `resume`, `answer`, `confirm`, `cancel`, `reconcile`, `recover`, and `delete_session`. Compose store/policy/scheduler/dispatcher/outbox without adding business prompts. `stream` reads durable events and appends connection-only control events.

- [x] **Step 5: Run Runtime tests**

Run: `pytest -q tests/test_agent_runtime_dispatcher.py tests/test_agent_runtime_scheduler.py tests/test_agent_runtime_policy.py tests/test_agent_runtime_recovery.py`

Expected: PASS.

- [x] **Step 6: Commit Runtime facade**

Result: 8/8 dispatcher tests GREEN, 242/242 full regression GREEN.

---

## Chunk 3: Agent Contracts, Context, Verification, and Projection

### Task 13: Upgrade AgentRegistry and SubagentProvider to V2

**Files:**
- Modify: `tradingagents/agent_harness/agents/base.py` (AgentDescriptor, AgentReply, LegacyAgentAdapter, Governed proxies)
- Modify: `tradingagents/agent_harness/agents/registry.py` (V2 registration + capability lookup + handoff metadata + require/health)
- Modify: `tradingagents/agent_harness/agents/subagent_provider.py` (record health errors on plugin load failure)
- Test: `tests/test_subagent_provider_v2.py` (new — added alongside existing V1 tests)

- [x] **Step 1: Write failing V2 registry tests**

- [x] **Step 2: Run tests to verify RED**

- [x] **Step 3: Implement V2 descriptors and registration**

- [x] **Step 4: Implement governed V1 adapters**

- [x] **Step 5: Run registry tests**

- [x] **Step 6: Commit registry V2**

Result: 10/10 subagent_provider tests GREEN, 270/270 full regression GREEN.

### Task 14: Implement ContextAssembler and TurnRepository

**Files:**
- Create: `tradingagents/agent_harness/core/context_assembler.py`
- Create: `tradingagents/agent_harness/core/turn_repository.py`
- Modify: `tradingagents/agent_harness/core/context.py`
- Modify: `tradingagents/agent_harness/core/session_manager.py`
- Test: `tests/test_d4_context_providers.py`
- Test: `tests/test_d8_context_memory.py`
- Test: `tests/test_agent_runtime_recovery.py`

- [ ] **Step 1: Write failing context/projection tests**

Require ordering: explicit task input → dependency artifacts → L1 chat → L2 prefs → L3 refs → system constraints. Test pending Runtime terminal overlay, projection receipt suppression, no failed/unverified draft in context, symbol/intent carry-forward, session accounting, and delete_session cleanup/cancellation.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_d4_context_providers.py tests/test_d8_context_memory.py tests/test_agent_runtime_recovery.py -k 'context or overlay or projection or session'`

Expected: FAIL because ContextAssembler/TurnRepository do not exist.

- [ ] **Step 3: Implement ContextAssembler**

Wrap existing ContextPriority providers rather than duplicating their budget logic. Resolve immutable dependency artifacts from RuntimeStore first. Read projection receipts and overlay only terminal, verified, not-yet-delivered runs. Return a typed AgentContextBundle.

- [ ] **Step 4: Implement TurnRepository**

Move session create/touch/token_total and L1/L2/L3 final projection out of Orchestrator. Use `append_projected_exchange` and mark Runtime projection DELIVERED only after applied/already-applied. Coordinate SessionManager delete with AgentRuntime cancellation and store cleanup.

- [ ] **Step 5: Run context/session tests**

Run: `pytest -q tests/test_d4_context_providers.py tests/test_d8_context_memory.py tests/test_p3_session_memory.py tests/test_session_manager.py tests/test_agent_runtime_recovery.py -k 'context or overlay or projection or session'`

Expected: PASS.

- [ ] **Step 6: Commit context/repository**

```bash
git add tradingagents/agent_harness/core/context_assembler.py tradingagents/agent_harness/core/turn_repository.py tradingagents/agent_harness/core/context.py tradingagents/agent_harness/core/session_manager.py tests/test_d4_context_providers.py tests/test_d8_context_memory.py tests/test_agent_runtime_recovery.py
git commit -m "feat(harness): assemble runtime context and projection"
```

### Task 15: Make PlannerAgent produce the fixed-backbone PlanGraph

**Files:**
- Modify: `tradingagents/agent_harness/agents/planner.py`
- Modify: `tradingagents/agent_harness/core/plan_template.py`
- Test: `tests/test_runtime_agents.py`
- Test: `tests/test_plan_template.py`

- [ ] **Step 1: Write failing Planner tests**

Test A-class empty domain graph, data-only, news-only, alpha-only, compare, multi-symbol fan-out, required/optional flags, local task keys, invalid LLM JSON fallback, capability catalog use, plan cache, and rejection of Planner-emitted verifier/synthesizer/tool actions.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_runtime_agents.py -k planner tests/test_plan_template.py`

Expected: FAIL because Planner still returns legacy plan arrays.

- [ ] **Step 3: Implement PlannerAgent V2**

Use `context.llm_executor` and registered AgentDescriptor capabilities. Return PlanGraph containing domain tasks only. Do not expose tool schemas or write commands to Planner. Keep deterministic fallback for symbols/intents; Runtime adds evidence verifier, synthesizer, and answer verifier.

- [ ] **Step 4: Run Planner tests**

Run: `pytest -q tests/test_runtime_agents.py -k planner tests/test_plan_template.py tests/test_llm_ptc_prompt.py`

Expected: PASS after legacy prompt tests are rewritten to PlanGraph semantics.

- [ ] **Step 5: Commit Planner migration**

```bash
git add tradingagents/agent_harness/agents/planner.py tradingagents/agent_harness/core/plan_template.py tests/test_runtime_agents.py tests/test_plan_template.py tests/test_llm_ptc_prompt.py
git commit -m "feat(harness): make planner emit domain task graphs"
```

### Task 16: Migrate Data, News, and Alpha agents

**Files:**
- Modify: `tradingagents/agent_harness/agents/data_agent.py`
- Modify: `tradingagents/agent_harness/agents/news_agent.py`
- Modify: `tradingagents/agent_harness/agents/alpha_agent.py`
- Test: `tests/test_runtime_agents.py`
- Test: `tests/test_d8_agents_llm.py`

- [ ] **Step 1: Write failing domain-Agent contract tests**

For each Agent assert: descriptor capabilities/tools; AgentTask input; ToolExecutor-only calls; LLMExecutor-only summaries; AgentScope enforcement; RESULT evidence/artifact refs/confidence/missing_items; Data quote+fundamentals concurrency; News lookback freshness; Alpha compute/evaluate behavior; and no direct registry/provider access.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_runtime_agents.py -k 'data or news or alpha'`

Expected: FAIL because current agents use direct tool/LLM injection and return legacy AgentResult.

- [ ] **Step 3: Implement V2 domain Agents**

DataAgent calls `context.tool_executor` concurrently for quote/fundamentals and optionally summarizes through LLMExecutor. NewsAgent validates `as_of`/lookback and emits QUESTION/HANDOFF_REQUEST only through AgentChannel. AlphaAgent selects list/compute/evaluate from objective/inputs without Orchestrator mapping. Persist raw tool outputs as artifacts before returning refs.

- [ ] **Step 4: Verify domain Agents**

Run: `pytest -q tests/test_runtime_agents.py -k 'data or news or alpha' tests/test_d8_agents_llm.py tests/test_data_provider_seam.py tests/test_news_provider_seam.py tests/test_alpha_provider_seam.py`

Expected: PASS.

- [ ] **Step 5: Commit domain Agents**

```bash
git add tradingagents/agent_harness/agents/data_agent.py tradingagents/agent_harness/agents/news_agent.py tradingagents/agent_harness/agents/alpha_agent.py tests/test_runtime_agents.py tests/test_d8_agents_llm.py
git commit -m "feat(harness): execute domain agents through runtime"
```

### Task 17: Migrate Verifier and Synthesizer with bounded repair

**Files:**
- Modify: `tradingagents/agent_harness/agents/verifier.py`
- Modify: `tradingagents/agent_harness/agents/synthesizer.py`
- Modify: `tradingagents/agent_harness/core/verification.py`
- Test: `tests/test_runtime_agents.py`
- Test: `tests/test_d8_l3_judge.py`
- Test: `tests/test_step30_verification_combined.py`
- Test: `tests/test_step34_synth_citation_retry.py`

- [ ] **Step 1: Write failing two-phase verification tests**

Cover evidence-phase VerifiedEvidenceRef issuance, rejected artifact IDs, required repair with FAIL_WAITER, optional repair with RESUME_WITH_FAILURE, synthesis accepting only verified refs, answer-phase groundedness/citation/claim audit, synthesis-only repair, repair limit exhaustion, and fail-open judge exception without bypassing deterministic L1/L2.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_runtime_agents.py -k 'verifier or synthesizer or repair'`

Expected: FAIL because verification/synthesis live in Orchestrator and legacy agents.

- [ ] **Step 3: Implement VerifierAgent phases**

Use task capability/input to select `VERIFY_EVIDENCE` or `VERIFY_ANSWER`. Keep deterministic structural/semantic/citation/claim-audit functions in `core/verification.py`; call judge only through LLMExecutor. Return REPAIR_REQUEST drafts with target, acceptance criteria, rejected artifacts, and on_reject.

- [ ] **Step 4: Implement SynthesizerAgent V2**

Reject plain EvidenceRef at schema boundary; accept VerifiedEvidenceRef only. Move synthesis prompt/citation retry out of Orchestrator. Return draft-answer artifact and RESULT; final user visibility waits for answer verifier.

- [ ] **Step 5: Run verification/synthesis regressions**

Run: `pytest -q tests/test_runtime_agents.py -k 'verifier or synthesizer or repair' tests/test_d8_l3_judge.py tests/test_step30_verification_combined.py tests/test_step34_synth_citation_retry.py tests/test_step36_chinese_numbers.py`

Expected: PASS.

- [ ] **Step 6: Commit verification/synthesis**

```bash
git add tradingagents/agent_harness/agents/verifier.py tradingagents/agent_harness/agents/synthesizer.py tradingagents/agent_harness/core/verification.py tests/test_runtime_agents.py tests/test_d8_l3_judge.py tests/test_step30_verification_combined.py tests/test_step34_synth_citation_retry.py
git commit -m "feat(harness): verify and synthesize through agents"
```

### Task 18: Implement dual-view TraceProjector

**Files:**
- Create: `tradingagents/agent_harness/runtime/projector.py`
- Modify: `tradingagents/agent_harness/core/surface.py`
- Test: `tests/test_agent_runtime_web_contract.py`
- Test: `tests/test_stream_surface_tagging.py`

- [ ] **Step 1: Write failing projection tests**

Assert durable events map to the complete compatibility payload matrix; seq/order is stable; plan precedes domain events; two verifier phases bracket synthesis; SYSTEM_COMMAND emits deterministic agent_final without verification; control `done/resume_complete` is not persisted; user view omits prompts/secrets; inspector view contains tasks/messages/evidence/timing/token data; cursor pagination maxes at 100.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_web_contract.py -k projector tests/test_stream_surface_tagging.py`

Expected: FAIL because TraceProjector is missing.

- [ ] **Step 3: Implement projections**

Map Runtime event types to existing SSE names/payloads and new `agent_progress`, `repair_started`, `handoff_requested`, `waiting_user`, `run_recovered`. Build connection-only `done`/`resume_complete` after durable replay. Inspector API projection reads only ingested/redacted rows and uses `(run_id, seq)` cursor.

- [ ] **Step 4: Run projection tests**

Run: `pytest -q tests/test_agent_runtime_web_contract.py -k projector tests/test_stream_surface_tagging.py tests/test_surface.py tests/test_surface_classification.py`

Expected: PASS.

- [ ] **Step 5: Commit TraceProjector**

```bash
git add tradingagents/agent_harness/runtime/projector.py tradingagents/agent_harness/core/surface.py tests/test_agent_runtime_web_contract.py tests/test_stream_surface_tagging.py
git commit -m "feat(harness): project runtime traces for ui and audit"
```

---

## Chunk 4: Assembly, Web Compatibility, Cutover, and Acceptance

### Task 19: Assemble Runtime in Harness without switching traffic

**Files:**
- Modify: `tradingagents/agent_harness/harness.py`
- Modify: `tradingagents/agent_harness/config/schema.py`
- Modify: `tradingagents/agent_harness/core/__init__.py`
- Modify: `tradingagents/agent_harness/runtime/__init__.py`
- Test: `tests/test_d4_harness_integration.py`
- Test: `tests/test_subagent_provider.py`
- Test: `tests/test_agent_control_wiring.py`

- [ ] **Step 1: Write failing Harness assembly tests**

Assert initialization order: config → registries/factories/memory → RuntimeStore migration → executors → agents → policy/scheduler/dispatcher/outbox/runtime → repositories/projector/coordinator. Assert all six V2 agents, required descriptors, runtime DB path, recovery scan, outbox start/stop, and compatibility facades `orchestrator.lifecycle/control_bus/plan_cache`.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_d4_harness_integration.py tests/test_subagent_provider.py tests/test_agent_control_wiring.py`

Expected: FAIL because Harness does not construct Runtime services.

- [ ] **Step 3: Add Runtime config**

Add typed limits to HarnessConfig: total tokens, deadline, dynamic tasks, messages, handoff depth, repair count, task timeout, outbox polling, lease seconds, and legacy V1 allowlist. Preserve env/YAML extra compatibility and provide conservative defaults from the spec.

- [ ] **Step 4: Assemble services without changing the public execution path**

Instantiate one RuntimeStore at `{data_dir}/agent_runtime.sqlite`; inject only governed executors into agents. Expose the new components as internal Harness attributes for tests, but keep `Harness.stream_chat`, `resume`, confirm handling, and `harness.orchestrator` on the existing production path until Task 24. Do not add a feature flag or runtime fallback selector.

- [ ] **Step 5: Run assembly tests**

Run: `pytest -q tests/test_d4_harness_integration.py tests/test_subagent_provider.py tests/test_agent_control_wiring.py tests/test_d8_config_loader.py`

Expected: PASS.

- [ ] **Step 6: Commit assembly**

```bash
git add tradingagents/agent_harness/harness.py tradingagents/agent_harness/config/schema.py tradingagents/agent_harness/core/__init__.py tradingagents/agent_harness/runtime/__init__.py tests/test_d4_harness_integration.py tests/test_subagent_provider.py tests/test_agent_control_wiring.py
git commit -m "feat(harness): assemble supervised agent runtime"
```

### Task 20: Implement TurnCoordinator and Tier 1 benchmark

**Files:**
- Create: `tradingagents/agent_harness/core/turn_coordinator.py`
- Modify: `tradingagents/agent_harness/core/short_circuit.py`
- Test: `tests/test_turn_coordinator.py`
- Test: `tests/test_agent_runtime_benchmarks.py`

- [ ] **Step 1: Write failing coordinator tests**

Test direct reads call ToolExecutor/ResultFormatter with zero LLM; writes create SYSTEM_COMMAND; analysis creates AGENT_ANALYSIS; carry-forward uses ContextAssembler; active run returns busy unless answer/confirm/resume; lifecycle/control callbacks fire; connection events always end with done.

- [ ] **Step 2: Add failing deterministic benchmarks**

Run 100 iterations with fake zero/known-delay tools. Capture legacy baseline before replacing production call, then assert new Tier 1 median overhead ≤5ms and p95 ≤1.10x baseline, with exactly zero LLM calls. Add a 100-iteration Runtime scheduling benchmark with p95 framework overhead ≤50ms.

- [ ] **Step 3: Run tests to verify RED**

Run: `pytest -q tests/test_turn_coordinator.py tests/test_agent_runtime_benchmarks.py`

Expected: FAIL because TurnCoordinator is missing.

- [ ] **Step 4: Implement TurnCoordinator**

Keep only top-level routing, turn lifecycle, Tier 1 direct call, Runtime run creation/control, and event forwarding. `short_circuit.py` becomes a thin Tier 1 adapter over Router/CommandResolver/ToolExecutor/ResultFormatter or is removed after callers migrate; it must not call ToolRegistry directly. Tests instantiate TurnCoordinator directly; do not wire Harness traffic yet.

- [ ] **Step 5: Run coordinator and benchmark tests**

Run: `pytest -q tests/test_turn_coordinator.py tests/test_agent_runtime_benchmarks.py tests/test_tier_degrade.py tests/test_short_circuit_multi.py tests/test_short_circuit_multi_e2e.py`

Expected: PASS and benchmark thresholds met.

- [ ] **Step 6: Commit coordinator**

```bash
git add tradingagents/agent_harness/core/turn_coordinator.py tradingagents/agent_harness/core/short_circuit.py tests/test_turn_coordinator.py tests/test_agent_runtime_benchmarks.py
git commit -m "feat(harness): coordinate turns through agent runtime"
```

### Task 21: Migrate Workflow services and legacy checkpoint import

**Files:**
- Modify: `tradingagents/agent_harness/core/workflow_spec_registry.py`
- Modify: `tradingagents/agent_harness/core/post_execute_workflow.py`
- Modify: `tradingagents/agent_harness/core/classify_plan_execute_workflow.py`
- Modify: `tradingagents/agent_harness/core/parallel_fetch_workflow.py`
- Modify: `tradingagents/agent_harness/harness.py`
- Test: `tests/test_workflow_spec_registry.py`
- Test: `tests/test_step31_post_execute_workflow.py`
- Test: `tests/test_agent_runtime_recovery.py`

- [ ] **Step 1: Write failing WorkflowServices tests**

Require graph/list/YAML/load behavior without any `_plan/_execute/_verify/_synthesize/_call_tool` access. Workflow templates may call ToolExecutor/AgentRuntime/TraceProjector through a typed `WorkflowServices` bundle but cannot form a second production chat path.

- [ ] **Step 2: Write failing legacy checkpoint tests**

Cover schemas with/without `workflow_name`, latest milestone selection, migration key uniqueness, scan after `set_checkpoint_store`, missing original message, replacement run CAS under race/crash, repeated resume returning the same active or terminal run, and no modification to web_runs/LangGraph checkpoints.

- [ ] **Step 3: Run tests to verify RED**

Run: `pytest -q tests/test_workflow_spec_registry.py tests/test_step31_post_execute_workflow.py tests/test_agent_runtime_recovery.py -k 'workflow or legacy'`

Expected: FAIL because workflows bind Orchestrator internals and legacy import is absent.

- [ ] **Step 4: Implement WorkflowServices and legacy importer**

Change constructors to accept focused services. Add `LegacyCheckpointImporter` owned by AgentRuntime; trigger it only when Harness receives the legacy store. Keep workflow endpoints as inspection/template surfaces.

- [ ] **Step 5: Verify workflow/recovery tests**

Run: `pytest -q tests/test_workflow_spec_registry.py tests/test_step31_post_execute_workflow.py tests/test_step32_classify_plan_execute.py tests/test_step33_workflow_yaml.py tests/test_step35_workflow_viz.py tests/test_agent_runtime_recovery.py -k 'workflow or legacy'`

Expected: PASS.

- [ ] **Step 6: Commit workflow/legacy migration**

```bash
git add tradingagents/agent_harness/core/workflow_spec_registry.py tradingagents/agent_harness/core/post_execute_workflow.py tradingagents/agent_harness/core/classify_plan_execute_workflow.py tradingagents/agent_harness/core/parallel_fetch_workflow.py tradingagents/agent_harness/harness.py tests/test_workflow_spec_registry.py tests/test_step31_post_execute_workflow.py tests/test_agent_runtime_recovery.py
git commit -m "refactor(harness): bind workflows to runtime services"
```

### Task 22: Adapt FastAPI endpoints to Runtime contracts

**Files:**
- Create: `web/harness_runtime_api.py`
- Test: `tests/test_agent_runtime_web_contract.py`
- Test: `tests/test_harness_event_modes.py`
- Test: `tests/test_step19_multi_session_api.py`
- Test: `tests/test_harness_sse_e2e.py`

- [ ] **Step 1: Write failing endpoint contract tests**

Cover chat old body plus reply fields; resume session/run/after_seq selection; session CRUD/fork/delete; confirm decision-only behavior; grant_all; batch; poll `since` and `after_seq` mutual exclusion/fixed run_id; cache stats; workflow endpoints; status/health; trace cursor/redaction/auth boundary; reconcile CAS/idempotency; and all documented 400/404/409 cases.

- [ ] **Step 2: Write failing SSE ordering/replay tests**

Assert envelope fields, persisted business seq, connection-only resume_complete/done, no replayed old done, plan/domain/verify/synth ordering, SYSTEM_COMMAND ordering, parallel interleaving allowance, duplicate replay dedupe, WAITING close behavior, and old event payload keys consumed by `harness.js`.

- [ ] **Step 3: Run tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_web_contract.py tests/test_harness_event_modes.py tests/test_harness_sse_e2e.py`

Expected: FAIL because routes still call old checkpoints/tools/Orchestrator internals.

- [ ] **Step 4: Implement unmounted route adapters**

Implement framework-light handler/stream functions receiving Harness/TurnCoordinator explicitly. Preserve paths' old request/response fields in the adapter contracts. Chat/resume/poll/batch use TurnCoordinator/TraceProjector; confirm only submits a decision; trace/reconcile and deterministic poll selection follow the spec. Test these functions directly with FastAPI test fixtures, but do not import or mount them from `web/app.py` until Task 24.

- [ ] **Step 5: Run web contract regressions**

Run: `pytest -q tests/test_agent_runtime_web_contract.py tests/test_harness_event_modes.py tests/test_harness_sse_e2e.py tests/test_step19_multi_session_api.py tests/test_harness_audit_fixes.py`

Expected: PASS.

- [ ] **Step 6: Commit web adapters**

```bash
git add web/harness_runtime_api.py tests/test_agent_runtime_web_contract.py tests/test_harness_event_modes.py tests/test_harness_sse_e2e.py tests/test_step19_multi_session_api.py
git commit -m "feat(web): prepare agent runtime api adapters"
```

### Task 23: Update Harness UI for dual-view messages

**Files:**
- Modify: `web/static/harness.js`
- Modify: `web/static/harness.html`
- Modify: `web/static/agent.css`
- Test: `tests/test_web_static.py`
- Test: `tests/test_agent_runtime_web_contract.py`

- [ ] **Step 1: Write failing static/UI contract tests**

Require summary handling for `agent_progress`, `repair_started`, `handoff_requested`, `waiting_user`, and `run_recovered`; reply_to_message_id submission; run_id-fixed poll/resume; Inspector pagination; no hidden prompt/secret rendering; confirm operation/approval IDs; and duplicate `(run_id or turn_id, seq)` suppression.

- [ ] **Step 2: Run static tests to verify RED**

Run: `pytest -q tests/test_web_static.py tests/test_agent_runtime_web_contract.py -k 'frontend or static or inspector or waiting'`

Expected: FAIL because the client does not know Runtime events/cursors.

- [ ] **Step 3: Implement client event/state handling**

Keep chat concise: one progress row per task, repair/handoff badges, explicit waiting prompt, and final answer. Inspector fetches the trace API only when opened and paginates. Never render arbitrary payload JSON into HTML; use existing safe markdown/text helpers.

- [ ] **Step 4: Run JS/static checks**

Run: `node --check web/static/harness.js`

Run: `pytest -q tests/test_web_static.py tests/test_agent_runtime_web_contract.py -k 'frontend or static or inspector or waiting'`

Expected: PASS.

- [ ] **Step 5: Commit UI compatibility**

```bash
git add web/static/harness.js web/static/harness.html web/static/agent.css tests/test_web_static.py tests/test_agent_runtime_web_contract.py
git commit -m "feat(web): show supervised agent progress and trace"
```

### Task 24: Perform the one-time production cutover

**Files:**
- Replace: `tradingagents/agent_harness/core/orchestrator.py`
- Modify: `tradingagents/agent_harness/core/__init__.py`
- Modify: `tradingagents/agent_harness/harness.py`
- Modify: `web/app.py`
- Modify/remove production use: `tradingagents/agent_harness/ptc.py`
- Modify/remove production use: `tradingagents/agent_harness/core/prefetch.py`
- Test: `tests/test_agent_runtime_architecture.py`
- Modify: `tests/test_d4_orchestrator.py`
- Modify: `tests/test_ptc_orchestrator_integration.py`
- Modify: `tests/test_stage2_regressions.py`
- Modify: `tests/test_multi_session_isolation.py`
- Modify: `tests/test_llm_ptc_prompt.py`
- Modify: `tests/test_synthesize_fastpath.py`
- Modify: `tests/test_lifecycle_wiring.py`
- Modify: `tests/test_tier_degrade.py`
- Modify: `tests/test_step40_synth_turn_retry.py`
- Modify: `tests/test_d8_l3_judge.py`

- [ ] **Step 1: Write failing architecture constraints**

Use AST/import inspection to require: coordinator/orchestrator combined non-comment LOC ≤600; no LLM provider/concrete tool/concrete Agent imports; no Agent→first-tool mapping; no CRUD args helpers; no direct tool invoke; AGENT tasks use AgentRegistry run; SYSTEM_COMMAND uses CommandExecutor; messages persist before schedule; EventBus is not message storage; and production `stream_chat` has exactly one Tier 2/3 Runtime call path.

- [ ] **Step 2: Run architecture tests to verify RED**

Run: `pytest -q tests/test_agent_runtime_architecture.py`

Expected: FAIL against the current 3,000+ line Orchestrator.

- [ ] **Step 3: Replace Orchestrator with compatibility facade**

`core/orchestrator.py` should re-export `Orchestrator = TurnCoordinator` and only compatibility types explicitly retained by the spec. Delete old planning/execution/verification/synthesis implementations and migrate all repository callers/tests in the same commit. Ensure Harness production `stream_chat` points only to TurnCoordinator.

- [ ] **Step 4: Mount Runtime Web adapters in the same cutover**

Replace the Harness route bodies in `web/app.py` with calls into `web/harness_runtime_api.py`; add trace/reconcile routes; change confirm from direct tool invocation to decision submission. There must be no commit where Web uses Runtime while Harness still exposes old Tier 2/3 execution.

- [ ] **Step 5: Remove obsolete production wiring**

Remove PTC/prefetch from chat execution. Keep generic modules only if still used by standalone Workflow tests; mark them non-production and ensure no import from TurnCoordinator/Runtime. Remove legacy Planner deprecation comments and Orchestrator-private Workflow adapters.

- [ ] **Step 6: Run architecture and focused behavior tests**

Run: `pytest -q tests/test_agent_runtime_architecture.py tests/test_turn_coordinator.py tests/test_runtime_agents.py tests/test_d4_orchestrator.py tests/test_ptc_orchestrator_integration.py`

Expected: PASS; the two previously known Tier 2/PTC failures are replaced by equivalent Runtime assertions, not skipped or xfailed.

- [ ] **Step 7: Commit cutover**

```bash
git add \
  tradingagents/agent_harness/core/orchestrator.py \
  tradingagents/agent_harness/core/__init__.py \
  tradingagents/agent_harness/harness.py \
  tradingagents/agent_harness/ptc.py \
  tradingagents/agent_harness/core/prefetch.py \
  web/app.py \
  tests/test_agent_runtime_architecture.py \
  tests/test_d4_orchestrator.py \
  tests/test_ptc_orchestrator_integration.py \
  tests/test_stage2_regressions.py \
  tests/test_multi_session_isolation.py \
  tests/test_llm_ptc_prompt.py \
  tests/test_synthesize_fastpath.py \
  tests/test_lifecycle_wiring.py \
  tests/test_tier_degrade.py \
  tests/test_step40_synth_turn_retry.py \
  tests/test_d8_l3_judge.py
git commit -m "refactor(harness): cut over to supervised agent runtime"
```

### Task 25: Run fault injection, full regression, and documentation verification

**Files:**
- Modify: `README.md` Harness architecture section
- Modify: `docs/superpowers/specs/2026-09-12-agent-harness-modularization.md` with superseded-link note only
- Test: all Harness/runtime/web tests

- [ ] **Step 1: Run focused Runtime suite**

Run:

```bash
pytest -q \
  tests/test_agent_runtime_models.py \
  tests/test_agent_message_ingestor.py \
  tests/test_agent_runtime_store.py \
  tests/test_agent_runtime_outbox.py \
  tests/test_agent_runtime_policy.py \
  tests/test_agent_runtime_scheduler.py \
  tests/test_agent_runtime_dispatcher.py \
  tests/test_tool_executor.py \
  tests/test_llm_executor.py \
  tests/test_runtime_agents.py \
  tests/test_turn_coordinator.py \
  tests/test_agent_runtime_recovery.py \
  tests/test_agent_runtime_web_contract.py \
  tests/test_agent_runtime_architecture.py \
  tests/test_agent_runtime_benchmarks.py
```

Expected: all pass, no skips except explicitly optional browser integration.

- [ ] **Step 2: Run required fault-injection matrix**

Run: `pytest -q tests/test_agent_runtime_recovery.py -k 'fault or crash or projection or legacy or wait'`

Expected: PASS for crashes before/after message commit, after enqueue boundary, after tool response/before outcome, after child completion/before wait resolution, after L1 append/before DELIVERED, after legacy replacement/before response, and after task completion/before run aggregation.

- [ ] **Step 3: Run full Harness regression**

Run: `pytest -q tests/test_*harness*.py tests/test_d[3-9]_*.py tests/test_step*.py tests/test_multi_session_isolation.py tests/test_web_static.py`

Expected: all pass; no known Tier 2/PTC failure remains.

- [ ] **Step 4: Run repository quality checks**

Run: `ruff check tradingagents/agent_harness web/app.py tests/test_agent_runtime*.py tests/test_runtime_agents.py tests/test_tool_executor.py tests/test_llm_executor.py tests/test_turn_coordinator.py`

Run: `node --check web/static/harness.js`

Run: `git diff --check`

Expected: all pass.

- [ ] **Step 5: Update architecture documentation**

Document the two execution paths (Tier 1 direct; Tier 2/3 Runtime), fixed backbone/dynamic repair limits, Runtime DB location/retention, recovery/NEEDS_RECONCILIATION operator flow, Inspector endpoint, plugin V2/V1 policy, and the absence of old Orchestrator fallback. Add a short superseded-by link to older modularization docs; do not rewrite historical specs.

- [ ] **Step 6: Commit docs and final verification evidence**

```bash
git add README.md docs/superpowers/specs/2026-09-12-agent-harness-modularization.md
git commit -m "docs(harness): document supervised agent runtime"
```

- [ ] **Step 7: Review final branch diff**

Run: `git status --short`

Run: `git diff --stat HEAD~25..HEAD`

Run: `git log --oneline --decorate -25`

Expected: only planned Runtime/Harness/Web/test/doc changes; every task has a focused commit; no unrelated user work is staged or reverted.
