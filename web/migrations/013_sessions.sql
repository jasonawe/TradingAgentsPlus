-- Roadmap §1.3 A2 — Session 元数据表。
-- 严格按 spec:`sessions(id, created_at, last_active, user_id, message_count, status)`
-- 不带 title / token_total / metadata 等扩展字段。
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL,
    last_active   TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_active
    ON sessions (user_id, last_active DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status
    ON sessions (status);
