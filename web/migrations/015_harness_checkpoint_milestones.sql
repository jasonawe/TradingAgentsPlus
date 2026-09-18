-- P2 (high availability): checkpoint granularity + partial replay.
--
-- Goal: instead of one row per session (last write wins), we now
-- keep a chain of milestone checkpoints so on crash we can resume
-- from the nearest successful milestone rather than replaying the
-- entire turn.
--
-- Schema change:
--   - PK becomes composite (session_id, milestone_id) — different
--     milestones for the same session become different rows.
--   - milestone_id is f"{node}:{emit_counter}" — emit_counter is
--     monotonic per (session_id, node), guaranteeing uniqueness.
--   - legacy single-row-per-session load() is preserved by reading
--     the highest milestone_id for the session.
--
-- Forward compatibility: existing harness_checkpoints rows from
-- pre-015 migration get a default milestone_id of "legacy:singleton"
-- so old data still resolves under load().

DROP TABLE IF EXISTS _harness_checkpoints_old;
ALTER TABLE harness_checkpoints RENAME TO _harness_checkpoints_old;

CREATE TABLE harness_checkpoints (
    session_id      TEXT NOT NULL,
    milestone_id    TEXT NOT NULL DEFAULT 'legacy:singleton',
    state_json      TEXT NOT NULL,
    node_position   TEXT NOT NULL,
    emitted_events  TEXT NOT NULL DEFAULT '[]',
    token_usage     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (session_id, milestone_id)
);

INSERT INTO harness_checkpoints
    (session_id, milestone_id, state_json, node_position,
     emitted_events, token_usage, created_at, updated_at)
SELECT
    session_id, 'legacy:singleton', state_json, node_position,
    emitted_events, token_usage, created_at, updated_at
FROM _harness_checkpoints_old;

DROP TABLE _harness_checkpoints_old;

CREATE INDEX IF NOT EXISTS idx_harness_checkpoints_session_updated
    ON harness_checkpoints(session_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_harness_checkpoints_updated_at
    ON harness_checkpoints(updated_at);
