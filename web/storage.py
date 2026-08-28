"""SQLite boundary and transactional schema migrations for the web platform."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_MIGRATION_LOCK = threading.RLock()


class SQLiteStore:
    """Small connection factory with one transactional migration boundary."""

    def __init__(self, path: str | Path, *, migrations_dir: str | Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrations_dir = Path(migrations_dir) if migrations_dir is not None else Path(__file__).with_name("migrations")
        self._closed = False
        self._prepare_legacy_snapshots()
        self._ensure_schema_version()
        self._migrate()
        self._migrate_legacy_snapshot_table()
        self._ensure_web_run_columns()
        self._ensure_market_quote_columns()

    def _prepare_legacy_snapshots(self) -> None:
        with _MIGRATION_LOCK:
            conn = self._connect()
            try:
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "snapshots" in names and "analysis_data_snapshots" not in names:
                    conn.execute("ALTER TABLE snapshots RENAME TO analysis_data_snapshots")
                    conn.commit()
            finally:
                conn.close()

    def _ensure_web_run_columns(self) -> None:
        expected = {"phase": "TEXT", "current_agent": "TEXT", "queued_at": "TEXT", "started_at": "TEXT", "finished_at": "TEXT", "signal": "TEXT", "report_id": "TEXT", "error_code": "TEXT", "error_message": "TEXT", "terminal_expires_at": "TEXT", "effective_quote_strategy_id": "TEXT", "effective_quote_provider_chain": "TEXT", "data_snapshot_id": "TEXT", "data_status": "TEXT", "reproducibility": "TEXT"}
        with _MIGRATION_LOCK:
            conn = self._connect()
            try:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_runs'").fetchone():
                    return
                existing = {row[1] for row in conn.execute("PRAGMA table_info(web_runs)")}
                for name, kind in expected.items():
                    if name not in existing:
                        conn.execute(f"ALTER TABLE web_runs ADD COLUMN {name} {kind}")
                conn.commit()
            finally:
                conn.close()

    def _ensure_market_quote_columns(self) -> None:
        expected = {"open": "REAL", "high": "REAL", "low": "REAL", "volume": "REAL", "market_status": "TEXT", "exchange": "TEXT", "raw_summary": "TEXT", "cache_status": "TEXT"}
        with _MIGRATION_LOCK:
            conn = self._connect()
            try:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_quotes'").fetchone():
                    return
                existing = {row[1] for row in conn.execute("PRAGMA table_info(market_quotes)")}
                for name, kind in expected.items():
                    if name not in existing:
                        conn.execute(f"ALTER TABLE market_quotes ADD COLUMN {name} {kind}")
                conn.commit()
            finally:
                conn.close()

    def _migrate_legacy_snapshot_table(self) -> None:
        with _MIGRATION_LOCK:
            conn = self._connect()
            try:
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "snapshots" in names and "analysis_data_snapshots" not in names:
                    conn.execute("ALTER TABLE snapshots RENAME TO analysis_data_snapshots")
                    conn.commit()
                elif "snapshots" in names and "analysis_data_snapshots" in names:
                    conn.execute("INSERT OR IGNORE INTO analysis_data_snapshots SELECT * FROM snapshots")
                    conn.execute("DROP TABLE snapshots")
                    conn.commit()
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _ensure_schema_version(self) -> None:
        with _MIGRATION_LOCK:
            conn = self._connect()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
                if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                    conn.execute("INSERT INTO schema_version(version) VALUES (0)")
                conn.commit()
            finally:
                conn.close()

    def _migrate(self) -> None:
        migrations = sorted(self.migrations_dir.glob("*.sql"))
        with _MIGRATION_LOCK:
            conn = self._connect()
            try:
                current = int(conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0])
                for migration in migrations:
                    try:
                        version = int(migration.name.split("_", 1)[0])
                    except (ValueError, IndexError):
                        continue
                    if version <= current:
                        continue
                    sql = migration.read_text(encoding="utf-8")
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        # Feed complete statements to sqlite without splitting
                        # semicolons inside quoted strings or comments.
                        statement = ""
                        for line in sql.splitlines(keepends=True):
                            statement += line
                            if sqlite3.complete_statement(statement):
                                conn.execute(statement)
                                statement = ""
                        if statement.strip():
                            conn.execute(statement)
                        conn.execute("UPDATE schema_version SET version = ?", (version,))
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    current = version
            finally:
                conn.close()

    @property
    def schema_version(self) -> int:
        with self.connection() as conn:
            return int(conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0])

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise RuntimeError("SQLiteStore is closed")
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self) -> None:
        self._closed = True
