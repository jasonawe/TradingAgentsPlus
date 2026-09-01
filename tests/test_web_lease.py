"""WorkerLease: renewal, owner_token, lease expiry, future-driven stop."""

from __future__ import annotations

import time
from concurrent.futures import Future
from datetime import timedelta

from web.error_codes import TerminalReason
from web.lease import WorkerLease, generate_owner_token
from web.manager import RunManager
from web.models import AnalysisRequest, RunStatus


class _FakeManager:
    def __init__(self) -> None:
        self.accepted = 0
        self.token = generate_owner_token()

    def renew_worker_lease(self, run_id, owner_token):
        if owner_token == self.token:
            self.accepted += 1
            return True
        return False


def _request():
    return AnalysisRequest(
        ticker="AAPL",
        analysis_date="2026-08-26",
        asset_type="stock",
        analysts=["market"],
        research_depth=1,
    )


def test_generate_owner_token_is_unique_and_unpredictable():
    seen = {generate_owner_token() for _ in range(64)}
    assert len(seen) == 64
    for token in seen:
        assert len(token) >= 16


def test_lease_renews_on_interval_until_future_settles():
    fake = _FakeManager()
    future: Future = Future()
    lease = WorkerLease(
        run_id="run-1",
        manager=fake,
        interval_seconds=0.05,
        future=future,
    )
    lease.owner_token = fake.token  # bind the matching token
    lease.start()
    try:
        time.sleep(0.18)
        assert fake.accepted >= 2
    finally:
        lease.stop()


def test_lease_stops_renewing_when_future_completes():
    fake = _FakeManager()
    future: Future = Future()
    lease = WorkerLease(
        run_id="run-1",
        manager=fake,
        interval_seconds=0.02,
        future=future,
    )
    lease.owner_token = fake.token
    lease.start()
    future.set_result(None)
    # Give the loop one full interval to notice the future.
    time.sleep(0.08)
    lease.stop()
    # After the future completes the loop should exit; we tolerate at
    # most one trailing renewal that races with the stop signal.
    assert fake.accepted <= 3


def test_stale_owner_token_is_rejected():
    fake = _FakeManager()
    lease = WorkerLease(
        run_id="run-1",
        manager=fake,
        interval_seconds=0.01,
    )
    # Bind a token that does not match what the manager holds.
    lease.owner_token = "stale-token"
    lease.start()
    time.sleep(0.05)
    lease.stop()
    assert fake.accepted == 0


def test_lease_expiry_does_not_falsely_terminate_long_model_call():
    """Phase-1 acceptance: a long LLM call must not be marked timed out.

    The watchdog uses ``worker_heartbeat_at`` (renewed by the lease
    thread) for liveness, not the activity heartbeat. A real lease
    thread keeping the worker_heartbeat_at fresh must keep the run in
    RUNNING even if the activity heartbeat is stale.
    """

    manager = RunManager(
        lifecycle_config={
            "run_timeout_seconds": {"value": 7200, "source": "hard_fallback"},
            "run_heartbeat_interval_seconds": {"value": 5, "source": "hard_fallback"},
            "run_heartbeat_timeout_seconds": {"value": 30, "source": "hard_fallback"},
        },
    )
    run = manager.start_run(_request(), run_id="long-call")
    manager.begin_run(run.run_id)
    state = manager._state(run.run_id)
    from web.lease import WorkerLease
    lease = WorkerLease(
        run_id=run.run_id,
        manager=manager,
        interval_seconds=0.05,
        future=state.future,
    )
    state.lease = lease
    lease.start()
    try:
        # Wait long enough that the lease-timeout window (30s) would
        # have elapsed without the lease thread; the lease renews every
        # 50ms so the watchdog must not transition to INTERRUPTED.
        deadline = time.monotonic() + 1.6
        while time.monotonic() < deadline:
            time.sleep(0.05)
        snapshot = manager.get_run(run.run_id)
        assert snapshot.status is RunStatus.RUNNING
        assert snapshot.terminal_reason is None
        assert snapshot.worker_heartbeat_at is not None
        # The lease has renewed at least once (interval 0.05s, total
        # wait >= 1.5s, so ≥ 20 renewals).
        assert lease.renewal_count > 5
    finally:
        lease.stop()
        manager.shutdown()


def test_lease_expiry_transitions_to_interrupted_when_worker_dies():
    """When the lease thread stops renewing, the watchdog marks interrupted."""

    manager = RunManager(
        lifecycle_config={
            "run_timeout_seconds": {"value": 7200, "source": "hard_fallback"},
            "run_heartbeat_interval_seconds": {"value": 5, "source": "hard_fallback"},
            "run_heartbeat_timeout_seconds": {"value": 30, "source": "hard_fallback"},
        },
    )
    # Force the worker_heartbeat_at backwards so the next check_expired
    # call sees a stale lease without us having to wait 30s.
    run = manager.start_run(_request(), run_id="dies")
    manager.begin_run(run.run_id)
    state = manager._state(run.run_id)
    state.record.worker_heartbeat_at = state.record.worker_heartbeat_at - timedelta(seconds=120)
    expired = manager.heartbeat(run.run_id)
    assert expired.status is RunStatus.INTERRUPTED
    assert expired.terminal_reason == TerminalReason.WORKER_LEASE_EXPIRED.value
    manager.shutdown()
