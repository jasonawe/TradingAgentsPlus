"""Step 38 — audit log housekeeping (R-G + R-H race fix).

Two responsibilities:

1. **Orphan audit row expiry** — when an audit row has been in
   ``pending`` or ``confirmed`` state for too long without the tool
   being invoked (LLM decided not to retry, tool invoke timed out,
   or the user closed the dialog), expire it so the audit viewer
   doesn't show "still waiting" rows that will never complete.

2. **Failed audit update retry** — when ``update_write_status``
   raises (DB locked, disk full, transient error), queue the
   pending (audit_id, status, error) tuple for retry so audit state
   eventually converges with actual tool execution state.

Both run as synchronous helpers that can be invoked from web app
startup, a scheduled APS job, or a unit test. The web app calls
``run_sweep()`` at startup to clean up stale rows from previous
runs.

Tunable knobs (env-driven):
- ``TRADINGAGENTS_PENDING_EXPIRY_SECONDS`` (default 600)
- ``TRADINGAGENTS_CONFIRMED_EXPIRY_SECONDS`` (default 600)
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)


def _default_db_path() -> Path:
    from tradingagents.default_config import web_runs_db_path
    return web_runs_db_path()


# In-memory queue of failed audit updates for retry. Each entry is
# (audit_id, target_status, error_msg, attempts). The sweeper retries
# each entry on the next run_sweep().
_failed_updates: list[tuple[int, str, str | None, int]] = []
_fail_lock = threading.Lock()


def queue_failed_update(
    audit_id: int,
    status: str,
    error: str | None = None,
) -> None:
    """Enqueue a (audit_id, status) update that failed earlier.

    Called from web/app.py confirm endpoint when update_write_status
    raised. The sweeper will attempt this update on the next run.
    """
    with _fail_lock:
        # Coalesce: don't enqueue duplicates for the same (audit_id, status).
        for entry in _failed_updates:
            if entry[0] == audit_id and entry[1] == status:
                return
        _failed_updates.append((audit_id, status, error, 0))


def expire_stale_pending(
    db_path: Path | None = None,
    *,
    min_age_seconds: int | None = None,
    now: float | None = None,
) -> int:
    """Expire pending audit rows older than ``min_age_seconds``.

    "Pending" means status=pending AND no tool invocation has started
    yet. Rows that sit in pending for >10 min usually reflect a user
    who closed the dialog without responding. Returns the count of
    rows expired.
    """
    db_path = db_path or _default_db_path()
    if not db_path.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - (
        min_age_seconds
        if min_age_seconds is not None
        else int(os.getenv("TRADINGAGENTS_PENDING_EXPIRY_SECONDS", "600"))
    )
    cutoff_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff)
    )
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                """UPDATE write_audit_log
                   SET status = 'expired', error = 'orphan: no confirmation'
                   WHERE status = 'pending' AND created_at < ?""",
                (cutoff_iso,),
            )
            conn.commit()
            n = cur.rowcount
        finally:
            conn.close()
    except Exception as e:
        _LOG.warning("expire_stale_pending failed: %s", e)
        return 0
    if n:
        _LOG.info("audit_sweeper: expired %d stale pending rows", n)
    return n


def expire_stale_confirmed(
    db_path: Path | None = None,
    *,
    min_age_seconds: int | None = None,
    now: float | None = None,
) -> int:
    """Expire confirmed audit rows that never transitioned to executed/failed.

    A row stuck in "confirmed" for >10 min usually means the tool
    invoke was interrupted (process crash between claim and invoke).
    Returns the count of rows expired.
    """
    db_path = db_path or _default_db_path()
    if not db_path.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - (
        min_age_seconds
        if min_age_seconds is not None
        else int(os.getenv("TRADINGAGENTS_CONFIRMED_EXPIRY_SECONDS", "600"))
    )
    cutoff_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff)
    )
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                """UPDATE write_audit_log
                   SET status = 'expired',
                       error = 'orphan: confirmed but never executed'
                   WHERE status = 'confirmed' AND created_at < ?""",
                (cutoff_iso,),
            )
            conn.commit()
            n = cur.rowcount
        finally:
            conn.close()
    except Exception as e:
        _LOG.warning("expire_stale_confirmed failed: %s", e)
        return 0
    if n:
        _LOG.info("audit_sweeper: expired %d stale confirmed rows", n)
    return n


def retry_failed_updates(
    db_path: Path | None = None,
    *,
    max_attempts: int = 3,
) -> int:
    """Drain the in-memory failed-update queue with exponential backoff.

    Each retry: attempts the update via a fresh connection; on success
    the entry is removed; on failure the attempt counter is bumped.
    Entries exceeding max_attempts are dropped (logged as warning).
    Returns the count of successful updates this round.
    """
    db_path = db_path or _default_db_path()
    if not db_path.exists():
        return 0
    succeeded = 0
    remaining: list[tuple[int, str, str | None, int]] = []
    with _fail_lock:
        queue = list(_failed_updates)
        _failed_updates.clear()
    for audit_id, status, error, attempts in queue:
        attempts += 1
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                if error is not None:
                    conn.execute(
                        "UPDATE write_audit_log SET status = ?, error = ? "
                        "WHERE id = ?",
                        (status, error, audit_id),
                    )
                else:
                    conn.execute(
                        "UPDATE write_audit_log SET status = ? WHERE id = ?",
                        (status, audit_id),
                    )
                conn.commit()
            finally:
                conn.close()
            succeeded += 1
        except Exception as e:
            _LOG.warning(
                "audit_sweeper retry failed (audit_id=%s, attempt=%d): %s",
                audit_id, attempts, e,
            )
            if attempts < max_attempts:
                remaining.append((audit_id, status, error, attempts))
    if remaining:
        with _fail_lock:
            _failed_updates.extend(remaining)
    return succeeded


def run_sweep(
    db_path: Path | None = None,
    *,
    min_age_seconds: int | None = None,
) -> dict[str, int]:
    """Run all sweeper tasks. Returns counts per task.

    Called from web/app.py startup and from the scheduled job. Idempotent
    and safe to call frequently.
    """
    return {
        "pending_expired": expire_stale_pending(
            db_path, min_age_seconds=min_age_seconds,
        ),
        "confirmed_expired": expire_stale_confirmed(
            db_path, min_age_seconds=min_age_seconds,
        ),
        "failed_retried": retry_failed_updates(db_path),
    }


__all__ = [
    "run_sweep",
    "expire_stale_pending",
    "expire_stale_confirmed",
    "retry_failed_updates",
    "queue_failed_update",
]
