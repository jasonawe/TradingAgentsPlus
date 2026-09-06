CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    body_md TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_symbol ON notes(symbol, asset_type, created_at DESC);
