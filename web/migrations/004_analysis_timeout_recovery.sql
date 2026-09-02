-- TradingAgentsPlus analysis timeout & stage recovery.
-- Adds separate worker-lease vs. analysis-activity timestamps, current
-- operation diagnostics, structured terminal reasons, retry linkage, and
-- per-stage artifacts so failed runs preserve the analyst reports that
-- already completed.
--
-- This migration must be idempotent on legacy databases that pre-date
-- web_runs (e.g. the snapshots-only fixtures); the matching Python hook
-- guards every ALTER TABLE with a ``PRAGMA table_info`` check.

CREATE TABLE IF NOT EXISTS analysis_run_artifacts (
    run_id TEXT NOT NULL,
    artifact_key TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    phase TEXT NOT NULL,
    agent TEXT,
    title TEXT NOT NULL,
    content_markdown TEXT NOT NULL,
    status TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, artifact_key),
    FOREIGN KEY (run_id) REFERENCES web_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_run_artifacts_sequence
    ON analysis_run_artifacts(run_id, sequence);

SELECT 1;
