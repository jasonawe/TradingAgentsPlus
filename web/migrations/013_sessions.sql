-- P1-A2 Session metadata table (per harness-improvement-roadmap §1.3)
--
-- One row per session_id. Provides the single-source-of-truth for
-- session lifecycle (created_at, last_active, message_count, status)
-- that today's harness lacks — currently `session_id` is just a string
-- parameter with no model behind it.
--
-- A6 (P2 work): this table will eventually replace both the LangGraph
-- SqliteSaver (data_dir/sessions/agent_*.db) AND the agent_harness L1
-- (.ta_cache/session_memory.sqlite). For now we live alongside both.

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT 'default',
    title           TEXT,
    created_at      TEXT NOT NULL,
    last_active     TEXT NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0,
    token_total     INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',  -- active / archived
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_active
    ON sessions(user_id, last_active DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_status
    ON sessions(status, last_active DESC);
