-- 017_workflow_checkpoints.sql
-- Step 42 — workflow ↔ checkpoint mapping.
--
-- Adds a workflow_name column to harness_checkpoints so a session can
-- carry checkpoints from multiple named workflows (e.g. classify-plan-execute
-- + post-execute when both run in the same turn). The composite primary
-- key becomes (session_id, workflow_name, milestone_id) for fine-grained
-- replay per workflow.

ALTER TABLE harness_checkpoints ADD COLUMN workflow_name TEXT NOT NULL DEFAULT 'default';

-- Drop the old composite-PK index that didn't include workflow_name
DROP INDEX IF EXISTS idx_harness_checkpoints_session_milestone;

-- New composite PK + per-workflow indexes
CREATE INDEX IF NOT EXISTS idx_harness_checkpoints_session_workflow
    ON harness_checkpoints(session_id, workflow_name);

CREATE INDEX IF NOT EXISTS idx_harness_checkpoints_session_workflow_node
    ON harness_checkpoints(session_id, workflow_name, node_position);
