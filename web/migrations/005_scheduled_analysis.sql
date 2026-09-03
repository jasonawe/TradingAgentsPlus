CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(symbol, asset_type)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_enabled
    ON scheduled_jobs(enabled);

CREATE TABLE IF NOT EXISTS scheduled_run_logs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES scheduled_jobs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    status TEXT NOT NULL,
    run_id TEXT,
    skip_reason TEXT,
    error TEXT,
    parameter_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_scheduled_run_logs_job
    ON scheduled_run_logs(job_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_scheduled_run_logs_run
    ON scheduled_run_logs(run_id);
