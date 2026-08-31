CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    run_id TEXT,
    ticker TEXT,
    asset_type TEXT,
    analysis_date TEXT,
    generated_at TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    rating TEXT,
    signal TEXT,
    output_language TEXT,
    summary_status TEXT,
    decision_preview TEXT,
    data_snapshot_id TEXT,
    provider TEXT,
    quick_model TEXT,
    deep_model TEXT,
    analysts_json TEXT,
    research_depth INTEGER,
    data_status TEXT,
    reproducibility TEXT,
    quote_strategy_id TEXT,
    effective_quote_provider_chain TEXT,
    root_name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'web',
    index_status TEXT NOT NULL DEFAULT 'indexed',
    path_state TEXT NOT NULL DEFAULT 'valid',
    updated_at TEXT NOT NULL,
    UNIQUE(root_name, relative_path)
);

CREATE INDEX IF NOT EXISTS idx_reports_generated ON reports(generated_at, report_id);
CREATE INDEX IF NOT EXISTS idx_reports_ticker ON reports(ticker);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_analysis_date ON reports(analysis_date);

CREATE TABLE IF NOT EXISTS report_index_outbox (
    report_id TEXT PRIMARY KEY,
    root_name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'not_configured',
    window_started_at TEXT,
    request_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_latency_ms REAL,
    last_error_code TEXT,
    last_error_message TEXT,
    updated_at TEXT NOT NULL
);
