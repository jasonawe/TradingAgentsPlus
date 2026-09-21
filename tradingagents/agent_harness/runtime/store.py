"""Task 3 — AgentRuntimeStore facade.

Compose focused repositories and expose a stable API surface. 本 Task 只实现
migrations bootstrap + connection accessor;各 repository(runs / tasks / events /
operations / usage / outbox / approvals)在后续 Task 填充。
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Iterator

from .persistence.db import open_connection

LOGGER = logging.getLogger(__name__)


class AgentRuntimeStore:
    """Process-local facade for the supervised AgentRuntime SQLite store."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path).resolve()
        self._conn: sqlite3.Connection | None = None
        # 立即初始化 — 触发 migration
        with open_connection(self._db_path) as conn:
            # 在迁移完成后关闭,我们后续按需重新 open
            pass
        LOGGER.info("AgentRuntimeStore initialised at %s", self._db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Acquire a connection. Migration 已执行,这里直接 open。"""
        # 单 connection 复用模式 — 短事务,串行使用
        if self._conn is None:
            from .persistence.db import _connect
            self._conn = _connect(self._db_path)
        try:
            yield self._conn
        except Exception:
            # connection 出错时关闭,下次重新打开
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Direct accessor — 假设调用方负责事务边界。"""
        if self._conn is None:
            from .persistence.db import _connect
            self._conn = _connect(self._db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


__all__ = ["AgentRuntimeStore"]
