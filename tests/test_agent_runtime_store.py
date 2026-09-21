"""Task 3 — AgentRuntimeStore schema/migration 测试。

覆盖 plan 要求:
- 每个 spec table 存在
- foreign key 约束
- 部分 active-run index
- (run_id, seq) uniqueness
- agent_task_waits.failure_policy
- outbox delivery_key NOT NULL
- operation/version fields
- usage-call uniqueness
- legacy migration uniqueness
- applying migrations twice 是 idempotent
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _import_store():
    from tradingagents.agent_harness.runtime.store import AgentRuntimeStore
    return AgentRuntimeStore


def _table_names(conn):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return {row[0] for row in cur.fetchall()}


def _index_names(conn):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
    return {row[0] for row in cur.fetchall()}


def _columns(conn, table):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


# ════════════════════════════════════════════════════════
# Step 1.1 — 必备 tables
# ════════════════════════════════════════════════════════

EXPECTED_TABLES = {
    "schema_migrations",
    "agent_runs",
    "agent_tasks",
    "agent_task_dependencies",
    "agent_task_waits",
    "agent_artifacts",
    "agent_messages",
    "runtime_events",
    "agent_outbox",
    "agent_operations",
    "agent_approvals",
    "agent_usage_reservations",
    "agent_legacy_interruptions",
}


def test_runtime_store_creates_all_spec_tables(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        tables = _table_names(conn)
        missing = EXPECTED_TABLES - tables
        assert not missing, f"missing tables: {missing}"
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# Step 1.2 — agent_runs 必备列 + active-run partial index
# ════════════════════════════════════════════════════════

AGENT_RUNS_COLUMNS = {
    "run_id", "session_id", "turn_id", "run_kind", "state", "route_json",
    "budgets_json", "final_result_json", "terminal_reason", "worker_id",
    "lease_expires_at", "heartbeat_at", "next_seq", "terminal_seq",
    "graph_revision", "session_projection_state", "session_projected_at",
    "version", "created_at", "updated_at",
}


def test_agent_runs_has_required_columns(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        cols = _columns(conn, "agent_runs")
        missing = AGENT_RUNS_COLUMNS - cols
        assert not missing, f"agent_runs missing columns: {missing}"
    finally:
        conn.close()


def test_active_run_partial_unique_index_exists(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        indexes = _index_names(conn)
        # 应该有一个 active-run 状态的部分唯一索引
        active_idx = [i for i in indexes if "active" in i.lower() or "session" in i.lower()]
        assert active_idx, f"no active-run partial index found in {indexes}"
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# Step 1.3 — (run_id, seq) uniqueness for messages + events
# ════════════════════════════════════════════════════════

def test_agent_messages_run_seq_unique(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        # 尝试插入 (run_id, seq) 重复 → 应失败
        conn.execute(
            "INSERT INTO agent_messages (message_id, run_id, turn_id, seq, task_id, sender, recipient, "
            "type, payload_json, evidence_refs_json, correlation_id, idempotency_key, "
            "execution_attempt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("m1", "r1", "t1", 1, "task1", "A", "B", "PROGRESS", "{}", "[]", "corr1", "idem1", 1, "2026-09-21T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO agent_messages (message_id, run_id, turn_id, seq, task_id, sender, recipient, "
            "type, payload_json, evidence_refs_json, correlation_id, idempotency_key, "
            "execution_attempt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("m2", "r1", "t1", 1, "task1", "A", "B", "PROGRESS", "{}", "[]", "corr2", "idem2", 1, "2026-09-21T00:00:00Z"),
        )
        conn.commit()
        assert False, "expected UNIQUE(run_id, seq) to reject duplicate seq"
    except sqlite3.IntegrityError:
        pass  # 预期
    finally:
        conn.close()


def test_runtime_events_run_seq_unique(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        conn.execute(
            "INSERT INTO runtime_events (event_id, run_id, seq, event_type, surface, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("e1", "r1", 1, "TASK_READY", "PUBLIC", "{}", "2026-09-21T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO runtime_events (event_id, run_id, seq, event_type, surface, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("e2", "r1", 1, "TASK_RUNNING", "PUBLIC", "{}", "2026-09-21T00:00:00Z"),
        )
        conn.commit()
        assert False, "expected UNIQUE(run_id, seq) on runtime_events"
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# Step 1.4 — agent_task_waits.failure_policy
# ════════════════════════════════════════════════════════

def test_agent_task_waits_has_failure_policy(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        cols = _columns(conn, "agent_task_waits")
        assert "failure_policy" in cols
        assert "wait_kind" in cols
        assert "waiter_task_id" in cols
        assert "child_task_id" in cols
        # UNIQUE(waiter_task_id, child_task_id)
        # 尝试插两条 waiter+child 相同
        conn.execute(
            "INSERT INTO agent_task_waits (wait_id, run_id, waiter_task_id, child_task_id, "
            "wait_kind, failure_policy, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("w1", "r1", "waiter1", "child1", "CHILD_TASK", "FAIL_RUN", "WAITING", "2026-09-21T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO agent_task_waits (wait_id, run_id, waiter_task_id, child_task_id, "
            "wait_kind, failure_policy, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("w2", "r1", "waiter1", "child1", "CHILD_TASK", "FAIL_RUN", "WAITING", "2026-09-21T00:00:00Z"),
        )
        conn.commit()
        assert False, "expected UNIQUE(waiter_task_id, child_task_id)"
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# Step 1.5 — outbox delivery_key NOT NULL + UNIQUE(destination, delivery_key)
# ════════════════════════════════════════════════════════

def test_agent_outbox_delivery_key_not_null(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        cols = _columns(conn, "agent_outbox")
        assert "delivery_key" in cols
        # 验证 NOT NULL — pragma_table_info 列叫 notnull(0/1)
        cur = conn.execute(
            "SELECT \"notnull\" FROM pragma_table_info('agent_outbox') WHERE name='delivery_key'"
        )
        row = cur.fetchone()
        assert row is not None, "delivery_key column not found"
        assert row[0] == 1, f"delivery_key should be NOT NULL, got notnull={row[0]}"
    finally:
        conn.close()


def test_agent_outbox_destination_delivery_key_unique(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        base = ("ob1", "r1", "task1", "msg1", "evt1", "key1", "ToolExecutor", "{}", "PENDING", 0,
                "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z")
        conn.execute(
            "INSERT INTO agent_outbox (outbox_id, run_id, task_id, message_id, source_event_id, "
            "delivery_key, destination, payload_json, state, attempts, available_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            base,
        )
        conn.execute(
            "INSERT INTO agent_outbox (outbox_id, run_id, task_id, message_id, source_event_id, "
            "delivery_key, destination, payload_json, state, attempts, available_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("ob2", "r1", "task1", "msg1", "evt1", "key1", "ToolExecutor", "{}", "PENDING", 0,
             "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z"),
        )
        conn.commit()
        assert False, "expected UNIQUE(destination, delivery_key)"
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# Step 1.6 — operation/version fields
# ════════════════════════════════════════════════════════

def test_agent_operations_has_version_and_idempotency(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        cols = _columns(conn, "agent_operations")
        for required in ("operation_id", "idempotency_key", "version", "args_hash",
                         "redacted_args_json", "state", "tool_name"):
            assert required in cols, f"agent_operations missing {required}"
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# Step 1.7 — usage-call uniqueness (call_id, provider_attempt_count)
# ════════════════════════════════════════════════════════

def test_usage_reservation_call_attempt_unique(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        base = ("rsv1", "call1", "r1", "task1", "PlannerAgent", 1, 1, "minimax-cn", "MiniMax-M2.7",
                "RESERVED", 1000, 500, 1, "2026-09-21T00:00:00Z",
                "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z")
        conn.execute(
            "INSERT INTO agent_usage_reservations (reservation_id, call_id, run_id, task_id, "
            "agent_name, execution_attempt, call_ordinal, provider, model, state, "
            "reserved_input_tokens, reserved_output_tokens, provider_attempt_count, "
            "lease_expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            base,
        )
        conn.execute(
            "INSERT INTO agent_usage_reservations (reservation_id, call_id, run_id, task_id, "
            "agent_name, execution_attempt, call_ordinal, provider, model, state, "
            "reserved_input_tokens, reserved_output_tokens, provider_attempt_count, "
            "lease_expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            ("rsv2", "call1", "r1", "task1", "PlannerAgent", 1, 1, "minimax-cn", "MiniMax-M2.7",
             "RESERVED", 1000, 500, 1, "2026-09-21T00:00:00Z",
             "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z"),
        )
        conn.commit()
        assert False, "expected UNIQUE(call_id, provider_attempt_count)"
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# Step 1.8 — legacy migration uniqueness
# ════════════════════════════════════════════════════════

def test_agent_legacy_interruptions_unique_migration_key(tmp_path):
    Store = _import_store()
    store = Store(tmp_path / "runtime.sqlite")
    conn = sqlite3.connect(tmp_path / "runtime.sqlite")
    try:
        conn.execute(
            "INSERT INTO agent_legacy_interruptions (migration_key, legacy_store_identity, "
            "session_id, milestone_id, node_position, state, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mk1", "orch_legacy", "sess1", "step1", "node1", "PENDING", "2026-09-21T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO agent_legacy_interruptions (migration_key, legacy_store_identity, "
            "session_id, milestone_id, node_position, state, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mk1", "orch_legacy", "sess2", "step2", "node2", "PENDING", "2026-09-21T00:00:00Z"),
        )
        conn.commit()
        assert False, "expected migration_key PRIMARY KEY"
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# Step 1.9 — idempotent migrations
# ════════════════════════════════════════════════════════

def test_applying_migrations_twice_is_idempotent(tmp_path):
    Store = _import_store()
    db_path = tmp_path / "runtime.sqlite"
    # 第一次
    Store(db_path)
    # 第二次:不抛错,且 schema_migrations 仍只有 1 行
    Store(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT COUNT(*) FROM schema_migrations")
        count = cur.fetchone()[0]
        # 至少有一个 001_initial.sql 记录
        assert count >= 1
        # 第二次后仍只有 1 条 001_initial
        cur = conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=1")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# Step 1.10 — schema_migrations 表存在 + 应用记录
# ════════════════════════════════════════════════════════

def test_schema_migrations_tracks_initial(tmp_path):
    Store = _import_store()
    db_path = tmp_path / "runtime.sqlite"
    Store(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        )
        rows = cur.fetchall()
        assert len(rows) >= 1, "no migrations recorded"
        assert rows[0][0] == 1, f"first migration version should be 1, got {rows[0][0]}"
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# Step 1.11 — connection policy (foreign keys + WAL)
# ════════════════════════════════════════════════════════

def test_connection_enables_foreign_keys(tmp_path):
    Store = _import_store()
    db_path = tmp_path / "runtime.sqlite"
    store = Store(db_path)
    # store 应暴露一个 connect() 方法
    assert hasattr(store, "connect") or hasattr(store, "connection"), \
        "AgentRuntimeStore should expose a connect/connection accessor"
    if hasattr(store, "connect"):
        ctx_or_conn = store.connect()
        if hasattr(ctx_or_conn, "__enter__"):
            conn = ctx_or_conn.__enter__()
            try:
                fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                assert fk == 1, f"foreign_keys should be ON, got {fk}"
            finally:
                ctx_or_conn.__exit__(None, None, None)
        else:
            conn = ctx_or_conn
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert fk == 1


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
