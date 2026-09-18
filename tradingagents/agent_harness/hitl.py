"""Stage C — 写操作 approval registry (persistent + in-memory fast path).

HITL 流程:
1. LLM 调用 write tool → tool 看到没有 approval → 返回 "AWAITING_CONFIRMATION" + 结构化 payload
2. Orchestrator 收到 → 写 audit log (status=pending) → 发 confirm_request event
3. 用户在 Drawer 点 确认/拒绝 → 前端 POST /confirm
4. 后端:
   - 更新 audit log (atomic CAS via claim_write_audit)
   - 若 approved → grant_approval 写入 tool_approvals SQLite 表
   - 重新调用 agent,LLM 再决定调同一个 tool,这次 tool 看到 approval → 真正执行

设计 (Step 38 R-E race fix):
- 持久化: 每次 grant_approval / consume_approval 写 SQLite,
  in-memory _approved 仅作 L1 cache。这样进程重启后,confirmed audit
  row 不会"复活"为 pending,避免重复 confirmation。
- 单次 approval,执行后由 consume_approval 设 consumed_at,行仍保留
  供审计;显式 revoke_session 清空。
- key = (session_id, tool_name, args_json) —— deterministic encoding 保证
  confirmation dialog 显示的 args 与后续 tool 调用的 args 生成相同 key。
- session-level grant-all 是 in-memory toggle (用户当前 session 一次性
  放行所有写操作),重启后默认关闭。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


_lock = threading.RLock()

# L1 in-memory cache: session_id -> set of (tool_name, args_json).
# Loaded from SQLite on first access for a session, kept in sync via
# grant_approval / consume_approval.
_approved: dict[str, set[tuple[str, str]]] = {}

# session_id -> bool. When True, _check_write_approval short-circuits to None
# (approved) for ALL write tools in this session, bypassing the per-call
# confirm dialog. Toggle via /api/harness/sessions/{sid}/grant_all.
_session_grant_all: dict[str, bool] = {}

# session_id -> True after the L1 cache has been primed from SQLite.
# Lets is_approved skip the DB hit on subsequent calls in the same
# process lifetime.
_cache_loaded: set[str] = set()


def _key_of(tool_name: str, tool_args: dict[str, Any]) -> tuple[str, str]:
    """生成稳定的 approval key(deterministic JSON 序列化)。"""
    return (tool_name, json.dumps(tool_args, ensure_ascii=False, sort_keys=True))


def _approval_db_path() -> Path:
    """Default location for the approvals SQLite table.

    Lives in web_runs.sqlite3 (alongside write_audit_log, tool_approvals
    is part of the same audit/approval subsystem).
    """
    from tradingagents.default_config import web_runs_db_path
    return web_runs_db_path()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tool_approvals table if missing.

    Idempotent — safe to call on every connection. The companion
    migration is 016_tool_approvals.sql; this fallback lets the
    registry work even when the migration hasn't been applied
    (e.g. test environments).
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tool_approvals (
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_json TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            consumed_at TIMESTAMP,
            audit_id INTEGER,
            PRIMARY KEY (session_id, tool_name, args_json)
        )"""
    )
    conn.commit()


def _load_session_into_cache(session_id: str) -> None:
    """One-shot: read all pending approvals for this session into L1.

    Used to avoid hitting SQLite on every is_approved call. After this
    runs once per session, the L1 cache stays in sync via
    grant/consume/revoke_session (which update both L1 and SQLite
    atomically).
    """
    if session_id in _cache_loaded:
        return
    db_path = _approval_db_path()
    if not db_path.exists():
        _cache_loaded.add(session_id)
        return
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_schema(conn)
            rows = conn.execute(
                """SELECT tool_name, args_json FROM tool_approvals
                   WHERE session_id = ? AND consumed_at IS NULL""",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        # If the DB is unavailable we'll fall back to L1 (empty) — the
        # caller will see "not approved" and re-gate, which is the
        # safe default.
        _cache_loaded.add(session_id)
        return
    s = _approved.setdefault(session_id, set())
    for tool_name, args_json in rows:
        s.add((tool_name, args_json))
    _cache_loaded.add(session_id)


def is_approved(session_id: str, tool_name: str, tool_args: dict[str, Any]) -> bool:
    """检查某次调用是否已被用户批准。

    Read path: L1 in-memory cache first, SQLite fallback for crash-
    recovery (L1 was wiped on restart but the row actually exists).
    """
    key = _key_of(tool_name, tool_args)
    with _lock:
        _load_session_into_cache(session_id)
        s = _approved.get(session_id)
        if s and key in s:
            return True
    db_path = _approval_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_schema(conn)
            row = conn.execute(
                """SELECT 1 FROM tool_approvals
                   WHERE session_id = ? AND tool_name = ?
                     AND args_json = ? AND consumed_at IS NULL""",
                (session_id, tool_name, key[1]),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    if row is None:
        return False
    # Re-prime L1 for this session so subsequent calls skip the DB.
    with _lock:
        s = _approved.setdefault(session_id, set())
        s.add(key)
        _cache_loaded.add(session_id)
    return True


def grant_approval(
    session_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    audit_id: int | None = None,
) -> None:
    """批准一次调用(单次有效,执行后由 consume_approval 标记 consumed)。

    Persists to tool_approvals SQLite table (R-E race fix) so the
    approval survives process restart. The L1 in-memory cache is
    updated in the same critical section so subsequent is_approved
    calls don't need a DB hit.
    """
    key = _key_of(tool_name, tool_args)
    with _lock:
        s = _approved.setdefault(session_id, set())
        s.add(key)
        _cache_loaded.add(session_id)
    db_path = _approval_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_schema(conn)
            conn.execute(
                """INSERT OR IGNORE INTO tool_approvals
                   (session_id, tool_name, args_json, created_at,
                    consumed_at, audit_id)
                   VALUES (?, ?, ?, ?, NULL, ?)""",
                (
                    session_id, tool_name, key[1],
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    audit_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Persistence failure — L1 still has the entry for the
        # lifetime of this process. Acceptable degradation; the
        # audit_sweeper will log this as a warning.
        pass


def consume_approval(session_id: str, tool_name: str, tool_args: dict[str, Any]) -> None:
    """消费一次 approval(执行成功后调用,避免重复执行)。

    Sets consumed_at = now() in SQLite so the audit viewer can see
    when the approval was used. The row is preserved (not deleted)
    for forensic purposes; orphan stale rows are expired by audit_sweeper.
    """
    key = _key_of(tool_name, tool_args)
    with _lock:
        s = _approved.get(session_id)
        if s:
            s.discard(key)
    db_path = _approval_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_schema(conn)
            conn.execute(
                """UPDATE tool_approvals
                   SET consumed_at = ?
                   WHERE session_id = ? AND tool_name = ?
                     AND args_json = ? AND consumed_at IS NULL""",
                (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    session_id, tool_name, key[1],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def revoke_session(session_id: str) -> None:
    """清除 session 的所有 approval(测试 / 强制 reset)。"""
    with _lock:
        _approved.pop(session_id, None)
        _cache_loaded.discard(session_id)
    db_path = _approval_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_schema(conn)
            conn.execute(
                "DELETE FROM tool_approvals WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def list_pending(session_id: str) -> list[dict[str, Any]]:
    """列出当前 session 已批准但未消费的(调试用)。

    Reads from L1 (which is kept in sync with SQLite via grant_approval
    / consume_approval / revoke_session).
    """
    with _lock:
        s = _approved.get(session_id, set())
        out = []
        for tool_name, args_json in s:
            try:
                args = json.loads(args_json)
            except json.JSONDecodeError:
                args = {}
            out.append({"tool_name": tool_name, "tool_args": args})
        return out


def count_pending(session_id: str | None = None) -> int:
    """Count pending (un-consumed) approvals, optionally per session.

    Used by the audit_sweeper for orphan detection and by the
    orchestrator for batch confirm requests.
    """
    db_path = _approval_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_schema(conn)
            if session_id is None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM tool_approvals "
                    "WHERE consumed_at IS NULL",
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM tool_approvals "
                    "WHERE session_id = ? AND consumed_at IS NULL",
                    (session_id,),
                ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def expire_pending(
    session_id: str,
    tool_name: str,
    args_json: str,
) -> bool:
    """Force-expire a pending approval (sweeper hook).

    Returns True if a row was expired. Used by audit_sweeper when
    an audit row in pending state has aged past the cutoff.
    """
    with _lock:
        s = _approved.get(session_id)
        if s:
            s.discard((tool_name, args_json))
    db_path = _approval_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            _ensure_schema(conn)
            cur = conn.execute(
                """DELETE FROM tool_approvals
                   WHERE session_id = ? AND tool_name = ?
                     AND args_json = ? AND consumed_at IS NULL""",
                (session_id, tool_name, args_json),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False


__all__ = [
    "is_approved",
    "grant_approval",
    "consume_approval",
    "revoke_session",
    "list_pending",
    "count_pending",
    "expire_pending",
]


def is_session_grant_all(session_id: str) -> bool:
    """True iff the user enabled "auto-approve all writes" for this session."""
    with _lock:
        return bool(_session_grant_all.get(session_id, False))


def grant_session_all(session_id: str) -> None:
    """Turn on auto-approve-all for this session (sticky for the session lifetime)."""
    with _lock:
        _session_grant_all[session_id] = True


def revoke_session_grant_all(session_id: str) -> None:
    """Turn off auto-approve-all (subsequent write tools will gate again)."""
    with _lock:
        _session_grant_all.pop(session_id, None)


def list_session_grants() -> dict[str, bool]:
    """Debug snapshot of session-level grant flags."""
    with _lock:
        return dict(_session_grant_all)
