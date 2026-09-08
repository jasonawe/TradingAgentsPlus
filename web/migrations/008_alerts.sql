CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    last_evaluated_at TEXT,
    last_triggered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol, asset_type, enabled);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(enabled, kind) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS alert_events (
    id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    kind TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    message TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_events_alert ON alert_events(alert_id, triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_symbol ON alert_events(symbol, asset_type, triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_unack ON alert_events(acknowledged_at) WHERE acknowledged_at IS NULL;
