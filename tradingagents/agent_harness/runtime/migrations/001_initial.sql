-- Task 3 — Runtime V2 initial schema
-- 13 tables covering runs / tasks / deps / waits / artifacts / messages / events /
-- outbox / operations / approvals / usage reservations / legacy interruptions / migrations.

-- ════════════════════════════════════════════════════════
-- agent_runs
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_runs (
  run_id                   TEXT PRIMARY KEY,
  session_id               TEXT NOT NULL,
  turn_id                  TEXT NOT NULL,
  run_kind                 TEXT NOT NULL,
  state                    TEXT NOT NULL,
  route_json               TEXT NOT NULL,
  budgets_json             TEXT NOT NULL,
  final_result_json        TEXT,
  terminal_reason          TEXT,
  worker_id                TEXT,
  lease_expires_at         TEXT,
  heartbeat_at             TEXT,
  next_seq                 INTEGER NOT NULL DEFAULT 1,
  terminal_seq             INTEGER,
  graph_revision           INTEGER NOT NULL DEFAULT 0,
  session_projection_state TEXT NOT NULL DEFAULT 'PENDING',
  session_projected_at     TEXT,
  version                  INTEGER NOT NULL DEFAULT 0,
  created_at               TEXT NOT NULL,
  updated_at               TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_agent_runs_active_session
  ON agent_runs (session_id)
  WHERE state IN ('PLANNING', 'RUNNING', 'WAITING_USER', 'WAITING_APPROVAL', 'NEEDS_RECONCILIATION');

CREATE INDEX idx_agent_runs_state ON agent_runs (state);

-- ════════════════════════════════════════════════════════
-- agent_tasks
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_tasks (
  task_id                  TEXT PRIMARY KEY,
  run_id                   TEXT NOT NULL,
  parent_task_id           TEXT,
  kind                     TEXT NOT NULL,
  agent_name               TEXT,
  system_handler           TEXT,
  capability               TEXT,
  objective                TEXT NOT NULL,
  inputs_json              TEXT NOT NULL,
  required                 INTEGER NOT NULL,
  state                    TEXT NOT NULL,
  execution_attempt        INTEGER NOT NULL DEFAULT 0,
  max_execution_attempts   INTEGER NOT NULL,
  resume_count             INTEGER NOT NULL DEFAULT 0,
  repair_count             INTEGER NOT NULL DEFAULT 0,
  handoff_depth            INTEGER NOT NULL DEFAULT 0,
  repair_of_task_id        TEXT,
  handoff_from_task_id     TEXT,
  worker_id                TEXT,
  lease_expires_at         TEXT,
  heartbeat_at             TEXT,
  result_json              TEXT,
  error_json               TEXT,
  version                  INTEGER NOT NULL DEFAULT 0,
  created_at               TEXT NOT NULL,
  updated_at               TEXT NOT NULL,
  FOREIGN KEY (run_id) REFERENCES agent_runs(run_id),
  FOREIGN KEY (parent_task_id) REFERENCES agent_tasks(task_id)
);

CREATE INDEX idx_agent_tasks_run_state ON agent_tasks (run_id, state);
CREATE INDEX idx_agent_tasks_parent ON agent_tasks (parent_task_id);
CREATE INDEX idx_agent_tasks_lease ON agent_tasks (lease_expires_at);

-- ════════════════════════════════════════════════════════
-- agent_task_dependencies
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_task_dependencies (
  run_id              TEXT NOT NULL,
  task_id             TEXT NOT NULL,
  depends_on_task_id  TEXT NOT NULL,
  condition           TEXT NOT NULL,
  PRIMARY KEY (run_id, task_id, depends_on_task_id),
  FOREIGN KEY (run_id, task_id) REFERENCES agent_tasks(run_id, task_id),
  FOREIGN KEY (run_id, depends_on_task_id) REFERENCES agent_tasks(run_id, task_id)
);

CREATE INDEX idx_task_deps_depends ON agent_task_dependencies (depends_on_task_id);

-- ════════════════════════════════════════════════════════
-- agent_task_waits
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_task_waits (
  wait_id          TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL,
  waiter_task_id   TEXT NOT NULL,
  child_task_id    TEXT NOT NULL,
  wait_kind        TEXT NOT NULL,
  failure_policy   TEXT NOT NULL,
  state            TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  resolved_at      TEXT,
  UNIQUE (waiter_task_id, child_task_id),
  FOREIGN KEY (waiter_task_id) REFERENCES agent_tasks(task_id),
  FOREIGN KEY (child_task_id)  REFERENCES agent_tasks(task_id)
);

CREATE INDEX idx_task_waits_state ON agent_task_waits (state);

-- ════════════════════════════════════════════════════════
-- agent_artifacts
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_artifacts (
  artifact_id        TEXT PRIMARY KEY,
  run_id             TEXT NOT NULL,
  producer_task_id   TEXT NOT NULL,
  source_type        TEXT NOT NULL,
  source_name        TEXT NOT NULL,
  content_json       TEXT NOT NULL,
  content_sha256     TEXT NOT NULL,
  as_of              TEXT,
  created_at         TEXT NOT NULL,
  FOREIGN KEY (run_id) REFERENCES agent_runs(run_id),
  FOREIGN KEY (producer_task_id) REFERENCES agent_tasks(task_id)
);

CREATE INDEX idx_artifacts_run ON agent_artifacts (run_id);
CREATE INDEX idx_artifacts_sha ON agent_artifacts (content_sha256);

-- ════════════════════════════════════════════════════════
-- agent_messages
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_messages (
  message_id           TEXT PRIMARY KEY,
  run_id               TEXT NOT NULL,
  turn_id              TEXT NOT NULL,
  seq                  INTEGER NOT NULL,
  task_id              TEXT NOT NULL,
  parent_task_id       TEXT,
  sender               TEXT NOT NULL,
  recipient            TEXT NOT NULL,
  type                 TEXT NOT NULL,
  payload_json         TEXT NOT NULL,
  evidence_refs_json   TEXT NOT NULL,
  causation_id         TEXT,
  correlation_id       TEXT NOT NULL,
  idempotency_key      TEXT NOT NULL UNIQUE,
  execution_attempt    INTEGER NOT NULL,
  created_at           TEXT NOT NULL,
  UNIQUE (run_id, seq),
  FOREIGN KEY (run_id) REFERENCES agent_runs(run_id),
  FOREIGN KEY (task_id) REFERENCES agent_tasks(task_id)
);

CREATE INDEX idx_messages_recipient ON agent_messages (recipient, type);
CREATE INDEX idx_messages_task ON agent_messages (task_id);

-- ════════════════════════════════════════════════════════
-- runtime_events
-- ════════════════════════════════════════════════════════
CREATE TABLE runtime_events (
  event_id     TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  event_type   TEXT NOT NULL,
  task_id      TEXT,
  message_id   TEXT,
  surface      TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  UNIQUE (run_id, seq),
  FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);

CREATE INDEX idx_events_surface ON runtime_events (surface);

-- ════════════════════════════════════════════════════════
-- agent_outbox
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_outbox (
  outbox_id        TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL,
  task_id          TEXT,
  message_id       TEXT,
  source_event_id  TEXT NOT NULL,
  delivery_key     TEXT NOT NULL,
  destination      TEXT NOT NULL,
  payload_json     TEXT NOT NULL,
  state            TEXT NOT NULL,
  attempts         INTEGER NOT NULL DEFAULT 0,
  available_at     TEXT NOT NULL,
  claimed_by       TEXT,
  claimed_at       TEXT,
  last_error       TEXT,
  created_at       TEXT NOT NULL,
  delivered_at     TEXT,
  UNIQUE (destination, delivery_key),
  FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
);

CREATE INDEX idx_outbox_state ON agent_outbox (state, available_at);

-- ════════════════════════════════════════════════════════
-- agent_operations
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_operations (
  operation_id          TEXT PRIMARY KEY,
  idempotency_key       TEXT NOT NULL UNIQUE,
  run_id                TEXT NOT NULL,
  task_id               TEXT NOT NULL,
  tool_name             TEXT NOT NULL,
  args_hash             TEXT NOT NULL,
  redacted_args_json    TEXT NOT NULL,
  side_effect_mode      TEXT NOT NULL,
  approval_id           TEXT,
  state                 TEXT NOT NULL,
  remote_idempotency_key TEXT,
  result_json           TEXT,
  error_json            TEXT,
  version               INTEGER NOT NULL DEFAULT 0,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  FOREIGN KEY (run_id) REFERENCES agent_runs(run_id),
  FOREIGN KEY (task_id) REFERENCES agent_tasks(task_id)
);

CREATE INDEX idx_operations_run_state ON agent_operations (run_id, state);

-- ════════════════════════════════════════════════════════
-- agent_approvals
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_approvals (
  approval_id   TEXT PRIMARY KEY,
  operation_id  TEXT NOT NULL UNIQUE,
  audit_id      INTEGER,
  decision      TEXT NOT NULL,
  decided_by    TEXT,
  expires_at    TEXT NOT NULL,
  version       INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  decided_at    TEXT,
  FOREIGN KEY (operation_id) REFERENCES agent_operations(operation_id)
);

-- ════════════════════════════════════════════════════════
-- agent_usage_reservations
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_usage_reservations (
  reservation_id        TEXT PRIMARY KEY,
  call_id               TEXT NOT NULL,
  run_id                TEXT NOT NULL,
  task_id               TEXT NOT NULL,
  agent_name            TEXT NOT NULL,
  execution_attempt     INTEGER NOT NULL,
  call_ordinal          INTEGER NOT NULL,
  provider              TEXT NOT NULL,
  model                 TEXT NOT NULL,
  state                 TEXT NOT NULL,
  reserved_input_tokens INTEGER NOT NULL,
  reserved_output_tokens INTEGER NOT NULL,
  actual_input_tokens   INTEGER,
  actual_output_tokens  INTEGER,
  provider_attempt_count INTEGER NOT NULL,
  lease_expires_at      TEXT,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  UNIQUE (call_id, provider_attempt_count),
  FOREIGN KEY (run_id) REFERENCES agent_runs(run_id),
  FOREIGN KEY (task_id) REFERENCES agent_tasks(task_id)
);

CREATE INDEX idx_usage_run ON agent_usage_reservations (run_id);

-- ════════════════════════════════════════════════════════
-- agent_legacy_interruptions
-- ════════════════════════════════════════════════════════
CREATE TABLE agent_legacy_interruptions (
  migration_key         TEXT PRIMARY KEY,
  legacy_store_identity TEXT NOT NULL,
  session_id            TEXT NOT NULL,
  workflow_name         TEXT,
  milestone_id          TEXT NOT NULL,
  node_position         TEXT NOT NULL,
  original_user_message TEXT,
  state                 TEXT NOT NULL,
  replacement_run_id    TEXT,
  imported_at           TEXT NOT NULL
);

CREATE INDEX idx_legacy_session ON agent_legacy_interruptions (session_id);
