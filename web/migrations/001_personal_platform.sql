CREATE TABLE IF NOT EXISTS web_runs (
    run_id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT,
    current_agent TEXT,
    progress REAL NOT NULL,
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    signal TEXT,
    report_id TEXT,
    error_code TEXT,
    error_message TEXT,
    terminal_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS watchlists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    id TEXT PRIMARY KEY,
    watchlist_id TEXT NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    note TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(watchlist_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_items_order ON watchlist_items(watchlist_id, position, id);
INSERT OR IGNORE INTO watchlists(id,name,version,created_at,updated_at) VALUES ('default','我的关注',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP);

CREATE TABLE IF NOT EXISTS market_quotes (
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    price REAL,
    previous_close REAL,
    change REAL,
    change_percent REAL,
    currency TEXT,
    as_of TEXT,
    fetched_at TEXT NOT NULL,
    freshness TEXT NOT NULL,
    source TEXT,
    payload_json TEXT,
    open REAL,
    high REAL,
    low REAL,
    volume REAL,
    market_status TEXT,
    exchange TEXT,
    raw_summary TEXT,
    cache_status TEXT,
    PRIMARY KEY(symbol, asset_type)
);

CREATE TABLE IF NOT EXISTS market_candles (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    source TEXT,
    PRIMARY KEY(symbol, interval, timestamp)
);

CREATE TABLE IF NOT EXISTS analysis_data_snapshots (
    run_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT,
    status TEXT NOT NULL DEFAULT 'recording',
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_run_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(run_id, seq),
    FOREIGN KEY(run_id) REFERENCES web_runs(run_id) ON DELETE CASCADE
);
