"""Agent 三层记忆 — L1 短期对话 / L2 用户偏好 / L3 历史引用。

L1 用 LangGraph SqliteSaver(per-session DB)
L2/L3 用 web_runs.sqlite3 中的 user_preferences / agent_references / write_audit_log 表
   (定义见 web/migrations/011_agent_memory.sql)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from tradingagents.dataflows.utils import safe_ticker_component


# ════════════════════════════════════════════════════════
# 路径工具
# ════════════════════════════════════════════════════════

def agent_data_dir(data_dir: str | Path) -> Path:
    """返回 Stage C agent 数据目录(~/.tradingagents/agent_general/)。"""
    p = Path(data_dir) / "agent_general"
    p.mkdir(parents=True, exist_ok=True)
    return p


def agent_memory_db_path(data_dir: str | Path) -> Path:
    """L2/L3 共用的 SQLite DB 路径(在 web_runs.sqlite3 同一文件,通过表名区分)。"""
    # 沿用 web/storage.py 的设计:L2/L3 表跟 web_runs 同 DB
    # 但调用方可以从 web/storage 拿 db_path,这里只返回默认值
    return Path(data_dir) / "web_runs.sqlite3"


def agent_session_db_path(data_dir: str | Path, session_id: str) -> Path:
    """L1 短期对话的 per-session SqliteSaver DB 路径。"""
    safe = safe_ticker_component(f"agent_{session_id}").upper()
    d = agent_data_dir(data_dir) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe}.db"


# ════════════════════════════════════════════════════════
# L1 — 短期对话(LangGraph SqliteSaver,per-session)
# ════════════════════════════════════════════════════════

@contextmanager
def get_session_checkpointer(
    data_dir: str | Path, session_id: str
) -> Iterator[SqliteSaver]:
    """LangGraph SqliteSaver context manager。

    仿 tradingagents/graph/checkpointer.py 的 get_checkpointer 模式,
    但 db 文件名加 agent_ 前缀,避免跟分析 run 的 checkpoint 混淆。
    """
    db = agent_session_db_path(data_dir, session_id)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    try:
        saver = SqliteSaver(conn)
        saver.setup()
        yield saver
    finally:
        conn.close()


def list_session_ids(data_dir: str | Path) -> list[str]:
    """列出所有 L1 短期对话 session_id(从 db 文件名提取)。"""
    d = agent_data_dir(data_dir) / "sessions"
    if not d.exists():
        return []
    ids = []
    for f in d.glob("AGENT_*.db"):
        # 文件名格式: AGENT_<safe_id>.db
        stem = f.stem  # AGENT_<safe_id>
        if stem.startswith("AGENT_"):
            ids.append(stem[len("AGENT_"):])
    return sorted(ids)


# ════════════════════════════════════════════════════════
# L2 — 用户偏好(SQLite user_preferences 表)
# ════════════════════════════════════════════════════════

def get_preference(db_path: Path, key: str) -> Any | None:
    """读单个偏好,JSON 反序列化。"""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT value FROM user_preferences WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]
    finally:
        conn.close()


def set_preference(
    db_path: Path, key: str, value: Any, source: str = "user"
) -> None:
    """写单个偏好(INSERT OR REPLACE)。

    value 会被 JSON 序列化。source ∈ {"user", "agent_inferred", "default"}。
    """
    payload = json.dumps(value, ensure_ascii=False)
    ts = datetime.utcnow().isoformat() + "Z"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """INSERT INTO user_preferences(key, value, source, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   source = excluded.source,
                   updated_at = excluded.updated_at""",
            (key, payload, source, ts),
        )
        conn.commit()
    finally:
        conn.close()


def list_preferences(db_path: Path) -> dict[str, Any]:
    """读所有偏好,返回 {key: value} dict。"""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT key, value, source FROM user_preferences ORDER BY key"
        ).fetchall()
        result: dict[str, Any] = {}
        for key, value, source in rows:
            try:
                result[key] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                result[key] = value
        return result
    finally:
        conn.close()


def delete_preference(db_path: Path, key: str) -> bool:
    """删一个偏好,返回是否真的删了。"""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("DELETE FROM user_preferences WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ════════════════════════════════════════════════════════
# L3 — 历史引用(SQLite agent_references 表,LanceDB 留接口)
# ════════════════════════════════════════════════════════

def store_reference(
    db_path: Path,
    *,
    session_id: str,
    nl_query: str,
    tool_name: str,
    tool_args: dict | None = None,
    tool_result: dict | None = None,
    tags: list[str] | None = None,
) -> int:
    """存一条引用,返回 id。"""
    ts = datetime.utcnow().isoformat() + "Z"
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """INSERT INTO agent_references
               (session_id, nl_query, tool_name, tool_args, tool_result, tags, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                nl_query,
                tool_name,
                json.dumps(tool_args, ensure_ascii=False) if tool_args else None,
                json.dumps(tool_result, ensure_ascii=False) if tool_result else None,
                " ".join(tags) if tags else None,
                ts,
            ),
        )
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def search_references(
    db_path: Path, query: str, limit: int = 5
) -> list[dict[str, Any]]:
    """关键词搜索(LIKE),返回最近 limit 条匹配。

    TODO:Day 4-5 升级为 LanceDB 向量检索(semantic search)。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        pattern = f"%{query}%"
        rows = conn.execute(
            """SELECT id, session_id, nl_query, tool_name, tool_args, tool_result, tags, created_at
               FROM agent_references
               WHERE nl_query LIKE ? OR tags LIKE ? OR tool_name LIKE ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        ).fetchall()
        return [_ref_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_references_by_session(
    db_path: Path, session_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """按 session 列引用(给前端历史对话列表用)。"""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """SELECT id, session_id, nl_query, tool_name, tool_args, tool_result, tags, created_at
               FROM agent_references
               WHERE session_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        return [_ref_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _ref_row_to_dict(row: tuple) -> dict[str, Any]:
    """DB row → dict(JSON 反序列化 args/result)。"""
    return {
        "id": row[0],
        "session_id": row[1],
        "nl_query": row[2],
        "tool_name": row[3],
        "tool_args": json.loads(row[4]) if row[4] else None,
        "tool_result": json.loads(row[5]) if row[5] else None,
        "tags": row[6].split() if row[6] else [],
        "created_at": row[7],
    }


__all__ = [
    "agent_data_dir",
    "agent_memory_db_path",
    "agent_session_db_path",
    "get_session_checkpointer",
    "list_session_ids",
    "get_preference",
    "set_preference",
    "list_preferences",
    "delete_preference",
    "store_reference",
    "search_references",
    "list_references_by_session",
]
