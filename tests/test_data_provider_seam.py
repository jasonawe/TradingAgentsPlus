"""P0-1 quote_provider seam — settings endpoint + persistence.

Verifies:
1. PATCH /api/settings/data-provider switches active provider
2. Invalid provider → 400
3. GET /api/settings exposes active_data_provider
4. Startup reads persisted provider from settings_repo
5. Seam unchanged (4 providers registered, switching works)
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def harness_app(tmp_path):
    """Build a FastAPI app with isolated tmp_path (mirrors test_web_api pattern)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from web.app import create_app
    from web.history import ReportHistory
    from web.manager import RunManager
    from web.config import DEFAULT_CONFIG
    import copy

    # Clear env override so settings_repo wins on restart
    os.environ.pop("TRADINGAGENTS_DATA_PROVIDER", None)

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["results_dir"] = str(tmp_path)
    manager = RunManager(db_path=tmp_path / "runs.sqlite3")
    history = ReportHistory(results_dir=tmp_path, cwd=tmp_path)

    app = create_app(config=config, manager=manager, history=history)
    return TestClient(app), tmp_path


def test_get_settings_includes_active_data_provider(harness_app):
    """GET /api/settings should expose active_data_provider field."""
    client, _ = harness_app
    resp = client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "fields" in body
    assert "active_data_provider" in body["fields"], list(body["fields"].keys())
    field = body["fields"]["active_data_provider"]
    assert field["value"] in ("yfinance", "eastmoney", "akshare", "alpha_vantage")


def test_patch_data_provider_switches_active(harness_app):
    """PATCH should switch active provider and persist."""
    from tradingagents.data.providers.registry import (
        get_active_provider_name, set_active_provider,
    )
    set_active_provider("eastmoney")

    client, _ = harness_app
    resp = client.patch(
        "/api/settings/data-provider",
        json={"provider": "yfinance"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "yfinance"
    assert "yfinance" in body["providers"]
    assert get_active_provider_name() == "yfinance"


def test_patch_invalid_provider_returns_400(harness_app):
    """Unknown provider name → 400 + lists known providers."""
    client, _ = harness_app
    resp = client.patch(
        "/api/settings/data-provider",
        json={"provider": "nonexistent_xyz"},
    )
    assert resp.status_code == 400, resp.text
    detail = str(resp.json().get("detail", ""))
    assert "yfinance" in detail and "eastmoney" in detail


def test_patch_then_get_reflects_change(harness_app):
    """After PATCH, GET should reflect the new active provider."""
    client, _ = harness_app
    resp = client.patch(
        "/api/settings/data-provider",
        json={"provider": "akshare"},
    )
    assert resp.status_code == 200
    resp = client.get("/api/settings")
    assert resp.json()["fields"]["active_data_provider"]["value"] == "akshare"


def test_persistence_across_app_restart(tmp_path):
    """Provider choice persists across app restart (settings_repo is source of truth)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from web.app import create_app
    from web.history import ReportHistory
    from web.manager import RunManager
    from web.config import DEFAULT_CONFIG
    from tradingagents.data.providers.registry import set_active_provider
    import copy

    os.environ.pop("TRADINGAGENTS_DATA_PROVIDER", None)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["results_dir"] = str(tmp_path)
    manager1 = RunManager(db_path=tmp_path / "runs.sqlite3")

    # First app — switch to akshare + persist
    app1 = create_app(config=config, manager=manager1,
                      history=ReportHistory(results_dir=tmp_path, cwd=tmp_path))
    c1 = TestClient(app1)
    set_active_provider("eastmoney")
    resp = c1.patch("/api/settings/data-provider", json={"provider": "akshare"})
    assert resp.status_code == 200

    # Second app on same tmp_path — should restore akshare from settings_repo
    manager2 = RunManager(db_path=tmp_path / "runs.sqlite3")
    app2 = create_app(config=config, manager=manager2,
                      history=ReportHistory(results_dir=tmp_path, cwd=tmp_path))
    c2 = TestClient(app2)
    resp = c2.get("/api/settings")
    field = resp.json()["fields"]["active_data_provider"]
    assert field["value"] == "akshare", f"expected akshare, got {field}"


def test_seam_invariant_unchanged():
    """The 4-provider registry contract is intact."""
    from tradingagents.data.providers.registry import PROVIDERS
    from tradingagents.data.providers.base import Provider

    expected = {"yfinance", "eastmoney", "akshare", "alpha_vantage"}
    assert set(PROVIDERS) == expected, set(PROVIDERS)
    for name, p in PROVIDERS.items():
        assert isinstance(p, Provider), f"{name} not a Provider"
        assert p.name == name, f"{name}.name = {p.name!r}"
