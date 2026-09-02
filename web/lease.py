"""In-process Worker lease that separates worker liveness from analysis activity.

The lease is a background thread bound to a Worker ``concurrent.futures.Future``.
While the future is alive it renews ``web_runs.worker_heartbeat_at`` every
``interval_seconds``. The lease holds a per-run ``owner_token`` that only
exists in this Python process; renewals that arrive without the matching
token are rejected so a stale Worker cannot resurrect a task that was
already terminated or reclaimed by another process.
"""

from __future__ import annotations

import logging
import secrets
import threading
from concurrent.futures import Future
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _RunManagerLike(Protocol):
    def renew_worker_lease(self, run_id: str, owner_token: str) -> bool: ...


def generate_owner_token() -> str:
    """Cryptographically random opaque token used to authenticate lease renewals."""

    return secrets.token_urlsafe(24)


class WorkerLease:
    """Background renewer for a single run's worker heartbeat.

    The lease is intentionally minimal: it owns the renewal thread, the
    owner token, and the stop signal. Lifecycle ownership stays with the
    RunManager (which decides when to start/stop leases) so concurrent
    transitions are arbitrated by the manager's own lock.
    """

    def __init__(
        self,
        *,
        run_id: str,
        manager: _RunManagerLike,
        interval_seconds: float,
        stop_event: threading.Event | None = None,
        future: Future[Any] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.run_id = run_id
        self._manager = manager
        self.interval_seconds = float(interval_seconds)
        self.owner_token = generate_owner_token()
        self._stop_event = stop_event or threading.Event()
        self._future = future
        self._thread: threading.Thread | None = None
        self._renewed_at: float = 0.0
        self._renewed_count: int = 0

    @property
    def token(self) -> str:
        return self.owner_token

    @property
    def renewal_count(self) -> int:
        return self._renewed_count

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        # First renewal happens synchronously so an immediate ``expired``
        # check inside the watchdog does not race against the thread's
        # first sleep.
        self._renew()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"tradingagents-worker-lease-{self.run_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            future = self._future
            if future is not None and future.done():
                # The Worker future is the source of truth for liveness.
                # Stop renewing once it has settled; the manager's terminal
                # transition will join this thread.
                break
            self._renew()

    def _renew(self) -> None:
        try:
            accepted = self._manager.renew_worker_lease(self.run_id, self.owner_token)
        except Exception:
            logger.exception("worker lease renewal failed for run %s", self.run_id)
            return
        if accepted:
            self._renewed_count += 1


__all__ = ["WorkerLease", "generate_owner_token"]
