-- 011_agent_memory.sql
-- Stage C (Finance General Agent) 记忆层 — 三张表
--   L2 user_preferences : 跨会话用户偏好(SQLite JSON)
--   L3 agent_references : 历史引用(nl_query → tool 结果),LanceDB 升级留接口
--   write_audit_log     : O10 写操作 audit log(HITL confirm 留痕)

-- ════════════════════════════════════════════════════════
-- L2 跨会话用户偏好
-- ════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS user_preferences (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'user',
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_user_preferences_source
    ON user_preferences(source, updated_at DESC);

-- ════════════════════════════════════════════════════════
-- L3 历史引用(关键词搜索,LanceDB 升级留接口)
-- ════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS agent_references (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    nl_query     TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    tool_args    TEXT,
    tool_result  TEXT,
    tags         TEXT,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_refs_session
    ON agent_references(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_refs_time
    ON agent_references(created_at DESC);

-- ════════════════════════════════════════════════════════
-- O10 写操作 audit log
-- ════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS write_audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT,
    user_message  TEXT,
    tool_name     TEXT NOT NULL,
    tool_args     TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT 'agent',
    confirmed_by  TEXT,
    status        TEXT NOT NULL,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_write_audit_session
    ON write_audit_log(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_write_audit_time
    ON write_audit_log(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_write_audit_status
    ON write_audit_log(status, created_at DESC);
