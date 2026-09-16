"""P7 tests: observability + health check + audit + metrics + failover + feishu."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.config.schema import HarnessConfig  # noqa: E402
from tradingagents.agent_harness.harness import Harness  # noqa: E402
from tradingagents.agent_harness.observability import (  # noqa: E402
    AuditLogger,
    FeishuAlerter,
    HealthChecker,
    Metrics,
    ProviderFailover,
    Tracer,
)


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


def test_audit_logger_writes_and_tails(tmp_path: Path) -> None:
    log = AuditLogger(data_dir=tmp_path)
    log.log("s1", "tool_call", {"name": "get_quote"})
    log.log("s1", "tier1_complete", {"latency_ms": 12})
    records = log.tail(10)
    assert len(records) == 2
    assert records[0]["event"] == "tool_call"
    assert records[1]["event"] == "tier1_complete"


def test_audit_logger_handles_missing_file(tmp_path: Path) -> None:
    log = AuditLogger(data_dir=tmp_path)
    assert log.tail(10) == []


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_snapshot_counts_and_averages() -> None:
    m = Metrics()
    m.observe("tool_call", 0.1)
    m.observe("tool_call", 0.2)
    m.observe("llm_call", 1.5, error=True)
    snap = m.snapshot()
    assert snap["counters"]["tool_call"]["count"] == 2
    assert snap["counters"]["tool_call"]["errors"] == 0
    assert snap["counters"]["llm_call"]["errors"] == 1


def test_metrics_reset_clears_state() -> None:
    m = Metrics()
    m.observe("x", 0.1)
    m.reset()
    assert m.snapshot()["counters"] == {}


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------


def test_tracer_start_end_records_span() -> None:
    t = Tracer()
    span = t.start("orchestrator.run", trace_id="abc")
    t.end(span, status="ok")
    snaps = t.snapshot()
    assert len(snaps) == 1
    assert snaps[0]["name"] == "orchestrator.run"
    assert snaps[0]["attrs"]["status"] == "ok"


# ---------------------------------------------------------------------------
# FeishuAlerter
# ---------------------------------------------------------------------------


def test_feishu_alerter_disabled_when_no_webhook(monkeypatch) -> None:
    monkeypatch.delenv("TRADINGAGENTS_FEISHU_WEBHOOK", raising=False)
    alerter = FeishuAlerter()
    assert alerter.enabled is False
    assert alerter.send("title", "content") is False


def test_feishu_alerter_enabled_when_env_set(monkeypatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_FEISHU_WEBHOOK", "https://open.feishu.cn/hook/x")
    alerter = FeishuAlerter()
    assert alerter.enabled is True


def test_feishu_alerter_send_failure_returns_false(monkeypatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_FEISHU_WEBHOOK", "https://open.feishu.cn/hook/x")
    alerter = FeishuAlerter()
    with patch("urllib.request.urlopen", side_effect=OSError("net")):
        assert alerter.send("title", "content") is False


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


def test_health_check_all_returns_expected_keys() -> None:
    h = Harness(HarnessConfig.from_env())
    checker = HealthChecker()
    payload = asyncio.run(checker.check_all(h))
    assert payload["ok"] is True
    assert "providers" in payload
    # Reads: 9 builtin reads (get_quote, get_quotes_batch, get_history,
    # get_fundamentals, get_news, list_alpha_factors, compute_alpha_factors,
    # evaluate_alpha, list_watchlist) + list_scheduled_tasks = 10.
    # Writes: 6 (alerts/notes × create/update/delete, scheduled_task
    # writes were dropped — see test_d3_tool_registry for reasons).
    # §P3-1 — harness now exposes 18 tools (10 read + 8 write),
    # up from 16 (10 read + 6 write) after watchlist write tools.
    assert payload["tools"]["total"] == 18
    assert payload["tools"]["read"] == 10
    assert payload["tools"]["write"] == 8  # §P3-1 added 2 watchlist writes
    assert len(payload["agents"]) == 6
    assert {"quant", "news", "alert"}.issubset(payload["plugins"])


def test_health_endpoint_helper_attaches_route() -> None:
    """The FastAPI helper should attach /api/harness/health to a TestClient-style app."""
    try:
        from fastapi import FastAPI  # type: ignore
        from starlette.testclient import TestClient  # type: ignore
    except ImportError:
        pytest.skip("fastapi / starlette not installed")
    h = Harness(HarnessConfig.from_env())
    test_app = FastAPI()
    from tradingagents.agent_harness.harness import mount_health_endpoint
    mount_health_endpoint(test_app, h)
    client = TestClient(test_app)
    resp = client.get("/api/harness/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # §P3-1 — same as the contract test above (18 tools total: 10 read + 8 write).
    assert body["tools"]["total"] == 18
    assert body["tools"]["write"] == 8


# ---------------------------------------------------------------------------
# Provider failover
# ---------------------------------------------------------------------------


def test_failover_falls_back_on_transient_error() -> None:
    from tradingagents.data.providers import registry as reg_mod
    from tradingagents.data.providers.registry import PROVIDERS
    from web.market_models import ProviderError, ProviderErrorCode, QuoteSnapshot
    from datetime import datetime, timezone

    class FlakyProvider:
        name = "flaky"
        def supports(self, symbol, asset_type, capability):
            return True
        def get_quote(self, symbol, asset_type):
            raise ProviderError(ProviderErrorCode.TIMEOUT, "transient boom")
        def get_candles(self, symbol, interval, start, end, asset_type):
            return []
        def get_identity(self, symbol, asset_type):
            from web.market_models import AssetIdentity
            return AssetIdentity(symbol=symbol, asset_type=asset_type)

    class HealthyProvider:
        name = "healthy"
        def supports(self, symbol, asset_type, capability):
            return True
        def get_quote(self, symbol, asset_type):
            return QuoteSnapshot(
                symbol=symbol, price=99.9, change=1.0, change_percent=1.0,
                volume=100, as_of=datetime.now(timezone.utc),
                fetched_at=datetime.now(timezone.utc),
            )
        def get_candles(self, symbol, interval, start, end, asset_type):
            return []
        def get_identity(self, symbol, asset_type):
            from web.market_models import AssetIdentity
            return AssetIdentity(symbol=symbol, asset_type=asset_type)

    original = dict(PROVIDERS)
    try:
        PROVIDERS["flaky"] = FlakyProvider()
        PROVIDERS["healthy"] = HealthyProvider()
        fo = ProviderFailover(primary="flaky", fallbacks=["healthy"])
        snap = fo.call("get_quote", "X", "stock")
        assert snap.price == 99.9
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(original)


def test_failover_propagates_terminal_error() -> None:
    from tradingagents.data.providers import registry as reg_mod
    from tradingagents.data.providers.registry import PROVIDERS
    from web.market_models import ProviderError, ProviderErrorCode

    class TerminalProvider:
        name = "terminal"
        def supports(self, symbol, asset_type, capability):
            return True
        def get_quote(self, symbol, asset_type):
            raise ProviderError(ProviderErrorCode.INVALID_SYMBOL, "bad symbol")
        def get_candles(self, symbol, interval, start, end, asset_type):
            return []
        def get_identity(self, symbol, asset_type):
            from web.market_models import AssetIdentity
            return AssetIdentity(symbol=symbol, asset_type=asset_type)

    original = dict(PROVIDERS)
    try:
        PROVIDERS["terminal"] = TerminalProvider()
        fo = ProviderFailover(primary="terminal", fallbacks=["terminal"])
        with pytest.raises(ProviderError):
            fo.call("get_quote", "X", "stock")
    finally:
        PROVIDERS.clear()
        PROVIDERS.update(original)
