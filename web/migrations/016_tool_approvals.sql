-- 016_tool_approvals.sql
-- Persistent approval registry for HITL (R-E race fix).
--
-- Replaces the in-memory `_approved` set in hitl.py so approvals survive
-- process restart. Composite primary key (session_id, tool_name, args_json)
-- gives idempotent grant_approval semantics — granting twice is a no-op.
--
-- consumed_at: NULL = pending (LLM may invoke tool); non-NULL = consumed
--              (tool already executed). NULL after a long delay means the
--              approval was orphaned (LLM never invoked) and the sweeper
--              should expire it.

CREATE TABLE IF NOT EXISTS tool_approvals (
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP,
    audit_id INTEGER,
    PRIMARY KEY (session_id, tool_name, args_json)
);

CREATE INDEX IF NOT EXISTS idx_tool_approvals_session
    ON tool_approvals(session_id);

CREATE INDEX IF NOT EXISTS idx_tool_approvals_audit
    ON tool_approvals(audit_id) WHERE audit_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tool_approvals_pending
    ON tool_approvals(created_at)
    WHERE consumed_at IS NULL;
