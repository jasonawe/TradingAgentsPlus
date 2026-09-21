"""Task 3 — Connection policy + migration loader.

负责:
- 创建 db 文件的 parent 目录
- enable foreign keys + WAL
- 用 importlib.resources 加载 packaged SQL
- 事务化执行 pending numbered migrations
- 提供 connect() context manager / connection accessor

设计:
- 连接策略:每个 AgentRuntimeStore 持有一个 ``sqlite3.Connection`` 实例,
  check_same_thread=False,busy_timeout 5s
- foreign_keys pragma 强制开启(WAL 不强制开启,默认用 journal)
- WAL 通过 ``PRAGMA journal_mode=WAL`` 显式设置(测试可关)
"""
from __future__ import annotations

import contextlib
import importlib.resources
import logging
import re
import sqlite3
from pathlib import Path
from typing import Iterator

LOGGER = logging.getLogger(__name__)


_MIGRATION_FILE_RE = re.compile(r"^(\d+)_([a-zA-Z0-9_]+)\.sql$")


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with the required policy applied."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 使用默认 isolation_level(deferred transaction)— 让 BEGIN/COMMIT 真正生效
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def _migration_files() -> list[tuple[int, str, str]]:
    """Return ``[(version, name, sql_text), ...]`` sorted by version."""
    pkg = importlib.resources.files("tradingagents.agent_harness.runtime.migrations")
    out: list[tuple[int, str, str]] = []
    for entry in pkg.iterdir():
        name = entry.name
        m = _MIGRATION_FILE_RE.match(name)
        if not m:
            continue
        version = int(m.group(1))
        label = m.group(2)
        sql_text = entry.read_text(encoding="utf-8")
        out.append((version, label, sql_text))
    out.sort(key=lambda x: x[0])
    return out


def _apply_pending_migrations(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Apply migrations that have not yet been recorded.

    Idempotent: applying twice does not re-execute recorded versions.
    """
    # Ensure schema_migrations exists even before first migration runs
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    cur = conn.execute("SELECT version FROM schema_migrations")
    applied = {row[0] for row in cur.fetchall()}

    applied_now: list[tuple[int, str]] = []
    from datetime import datetime, timezone

    for version, label, sql_text in _migration_files():
        if version in applied:
            continue
        LOGGER.info("applying migration %03d_%s", version, label)
        # 用 deferred transaction 包 executescript;CREATE TABLE 等语句会启动隐式 txn
        # 但 executescript 内部已经管理事务边界,这里直接调
        try:
            conn.executescript(sql_text)
        except Exception:
            raise
        # executescript 在 deferred isolation 下会自动 commit DDL;记录已应用
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (version, label, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        applied_now.append((version, label))
    return applied_now


@contextlib.contextmanager
def open_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Context manager that opens a connection with policy applied and runs pending migrations."""
    conn = _connect(db_path)
    try:
        _apply_pending_migrations(conn)
        yield conn
    finally:
        conn.close()


__all__ = ["open_connection", "_connect", "_apply_pending_migrations", "_migration_files"]
