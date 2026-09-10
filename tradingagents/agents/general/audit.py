"""Stage C Agent 写操作 audit log — O10 必做。

写操作前 → log_write(status="pending")
用户确认 → log_write(status="confirmed", confirmed_by=user_id)
执行成功 → log_write(status="executed")
执行失败 → log_write(status="failed", error=msg)
```

数据存在 web_runs.sqlite3.write_audit_log 表(011 迁移)。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


# ════════════════════════════════════════════════════════
# 路径
# ════════════════════════════════════════════════════════

def _default_db_path() -> Path:
    return Path.home() / ".tradingagents" / "web_runs.sqlite3"


# ════════════════════════════════════════════════════════
# log_write
# ════════════════════════════════════════════════════════

def log_write(
    db_path: Path | None = None,
    *,
    session_id: str | None = None,
    user_message: str | None = None,
    tool_name: str,
    tool_args: dict[str, Any],
    actor: str = "agent",
    confirmed_by: str | None = None,
    status: str = "pending",
    error: str | None = None,
) -> int:
    """记录一次写操作,返回 audit log id。

    Args:
        db_path: SQLite DB 路径(默认 ~/.tradingagents/web_runs.sqlite3)
        session_id: chat session ID
        user_message: 触发本次写操作的原始用户消息
        tool_name: 工具名
        tool_args: 工具参数(JSON 序列化)
        actor: 发起者("agent" / "user")
        confirmed_by: 确认人(用户 id),HITL 确认后填
        status: "pending" / "confirmed" / "rejected" / "executed" / "failed"
        error: 错误信息(失败时填)

    Returns:
        audit log id
    """
    if db_path is None:
        db_path = _default_db_path()

    ts = datetime.utcnow().isoformat() + "Z"
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """INSERT INTO write_audit_log
               (session_id, user_message, tool_name, tool_args,
                actor, confirmed_by, status, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                user_message,
                tool_name,
                json.dumps(tool_args, ensure_ascii=False),
                actor,
                confirmed_by,
                status,
                error,
                ts,
            ),
        )
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# 查询
# ════════════════════════════════════════════════════════

def list_writes(
    db_path: Path | None = None,
    session_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """查询 audit log(给管理 UI 用)。

    Args:
        db_path: DB 路径
        session_id: 可选 session 过滤
        status: 可选 status 过滤
        limit: 最大返回数

    Returns:
        列表,每条 dict 含 id / session_id / user_message / tool_name / tool_args /
        actor / confirmed_by / status / error / created_at
    """
    if db_path is None:
        db_path = _default_db_path()

    sql = "SELECT id, session_id, user_message, tool_name, tool_args, actor, confirmed_by, status, error, created_at FROM write_audit_log WHERE 1=1"
    params: list[Any] = []
    if session_id is not None:
        sql += " AND session_id = ?"
        params.append(session_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": r[0],
                "session_id": r[1],
                "user_message": r[2],
                "tool_name": r[3],
                "tool_args": json.loads(r[4]) if r[4] else {},
                "actor": r[5],
                "confirmed_by": r[6],
                "status": r[7],
                "error": r[8],
                "created_at": r[9],
            }
            for r in rows
        ]
    finally:
        conn.close()


def update_write_status(
    db_path: Path | None,
    audit_id: int,
    *,
    status: str,
    confirmed_by: str | None = None,
    error: str | None = None,
) -> bool:
    """更新 audit log 状态(用户确认 / 拒绝 / 执行后回填)。

    Returns:
        是否真的更新了
    """
    if db_path is None:
        db_path = _default_db_path()

    conn = sqlite3.connect(str(db_path))
    try:
        sets = ["status = ?"]
        params: list[Any] = [status]
        if confirmed_by is not None:
            sets.append("confirmed_by = ?")
            params.append(confirmed_by)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        params.append(audit_id)
        sql = f"UPDATE write_audit_log SET {', '.join(sets)} WHERE id = ?"
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


__all__ = [
    "log_write",
    "list_writes",
    "update_write_status",
]
