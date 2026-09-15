-- P1-7: Harness crash recovery — persist per-session state for resume.
--
-- When the server restarts (planned or crash), clients whose SSE
-- connection died mid-request can resume from the last checkpoint
-- instead of restarting the analysis.
--
-- Schema: one row per session_id (PRIMARY KEY). The state_json is
-- opaque to SQLite (json.loads on read); we keep it TEXT for forward
-- compatibility (don't need to add columns when state shape evolves).
--
-- node_position: which of the 5 nodes we're on (planning / executing /
-- observing / synthesizing / done). Resume replays emitted_events then
-- continues from this node.
--
-- TTL: cleanup policy is 24h, enforced by HarnessCheckpointStore.purge_stale.

CREATE TABLE IF NOT EXISTS harness_checkpoints (
    session_id      TEXT PRIMARY KEY,
    state_json      TEXT NOT NULL,           -- OrchestratorState JSON
    node_position   TEXT NOT NULL,           -- which node is current
    emitted_events  TEXT NOT NULL DEFAULT '[]',  -- JSON list of (event, payload)
    token_usage     TEXT NOT NULL DEFAULT '{}',  -- JSON summary from TokenUsageStore
    created_at      TEXT NOT NULL,           -- ISO8601
    updated_at      TEXT NOT NULL            -- ISO8601
);

CREATE INDEX IF NOT EXISTS idx_harness_checkpoints_updated_at
    ON harness_checkpoints(updated_at);
