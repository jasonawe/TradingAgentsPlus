from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from web.config import market_data_catalog, model_catalog
from web.repositories import SettingsRepository
from web.scheduled import (
    CronExpressionError,
    build_default_scheduled_request,
    infer_scheduled_request,
    next_fire_times,
    validate_cron_expression,
)
from web.storage import SQLiteStore


def test_pyproject_declares_apscheduler_as_a_direct_dependency():
    contents = Path("pyproject.toml").read_text(encoding="utf-8")
    assert '"apscheduler>=3.10,<4.0"' in contents


def test_pyproject_packages_sqlite_migrations_with_the_web_package():
    contents = Path("pyproject.toml").read_text(encoding="utf-8")
    assert 'web = ["static/*", "migrations/*.sql"]' in contents


def test_cron_validation_requires_exactly_five_standard_fields():
    assert validate_cron_expression("30 9 * * 1-5") == "30 9 * * 1-5"

    with pytest.raises(CronExpressionError, match="exactly 5 fields"):
        validate_cron_expression("0 30 9 * * 1-5")
    with pytest.raises(CronExpressionError, match="invalid cron expression"):
        validate_cron_expression("61 9 * * 1-5")


def test_next_fire_times_returns_three_timezone_aware_future_values():
    shanghai = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 9, 2, 8, 0, tzinfo=shanghai)
    assert next_fire_times("30 9 * * 1-5", now=now, timezone=shanghai) == [
        "2026-09-02T09:30:00+08:00",
        "2026-09-03T09:30:00+08:00",
        "2026-09-04T09:30:00+08:00",
    ]


def test_next_fire_times_excludes_the_exact_current_instant():
    shanghai = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 9, 2, 9, 30, tzinfo=shanghai)
    assert next_fire_times("30 9 * * 1-5", count=1, now=now, timezone=shanghai) == [
        "2026-09-03T09:30:00+08:00"
    ]


def test_infer_scheduled_request_reuses_parameters_but_refreshes_identity_and_date():
    latest = {
        "ticker": "MSFT",
        "analysis_date": "2026-09-01",
        "asset_type": "stock",
        "analysts": ["market", "news"],
        "research_depth": 5,
        "output_language": "English",
        "provider": "anthropic",
        "quick_model": "claude-quick",
        "deep_model": "claude-deep",
        "quote_strategy_id": "fallback-yfinance-alpha-vantage",
    }

    request, source = infer_scheduled_request(
        "btc-usd",
        "crypto",
        latest_request=latest,
        config=dict(DEFAULT_CONFIG),
        today=date(2026, 9, 2),
    )

    assert source == "last_successful"
    assert request.ticker == "BTC-USD"
    assert request.asset_type.value == "crypto"
    assert request.analysis_date == date(2026, 9, 2)
    assert [analyst.value for analyst in request.analysts] == ["market", "news"]
    assert request.research_depth == 5
    assert request.provider == "anthropic"
    assert request.quick_model == "claude-quick"
    assert request.deep_model == "claude-deep"
    assert request.output_language == "English"
    assert request.quote_strategy_id == "fallback-yfinance-alpha-vantage"


def test_default_scheduled_request_uses_catalog_defaults_all_analysts_and_today():
    config = dict(DEFAULT_CONFIG)
    _, model_defaults = model_catalog(config)
    quote_default = market_data_catalog(config)["quote_strategy_id"]["value"]

    request = build_default_scheduled_request(
        "btc-usd",
        "crypto",
        config=config,
        today=date(2026, 9, 2),
    )
    inferred, source = infer_scheduled_request(
        "btc-usd",
        "crypto",
        latest_request=None,
        config=config,
        today=date(2026, 9, 2),
    )

    assert source == "global_default"
    assert inferred == request
    assert request.ticker == "BTC-USD"
    assert request.analysis_date == date(2026, 9, 2)
    assert [analyst.value for analyst in request.analysts] == ["market", "social", "news"]
    assert request.research_depth == 1
    assert request.provider == model_defaults["provider"]
    assert request.quick_model == model_defaults["quick_model"]
    assert request.deep_model == model_defaults["deep_model"]
    assert request.output_language == model_defaults["output_language"]
    assert request.quote_strategy_id == quote_default


def test_default_scheduled_request_prefers_valid_persisted_output_language(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TRADINGAGENTS_OUTPUT_LANGUAGE", raising=False)
    config = {**DEFAULT_CONFIG, "output_language": "Chinese"}
    store = SQLiteStore(tmp_path / "scheduled.sqlite3")
    settings_repo = SettingsRepository(store)
    settings_repo.set("output_language", "Japanese")

    request = build_default_scheduled_request(
        "AAPL",
        "stock",
        config=config,
        settings=settings_repo.all(),
        today=date(2026, 9, 2),
    )

    assert request.output_language == "Japanese"
    store.close()


def test_default_scheduled_request_language_priority_validates_settings(monkeypatch):
    config = {**DEFAULT_CONFIG, "output_language": "Chinese"}
    settings = {"output_language": {"value": "Klingon", "source": "sqlite"}}
    monkeypatch.delenv("TRADINGAGENTS_OUTPUT_LANGUAGE", raising=False)

    request = build_default_scheduled_request(
        "AAPL", "stock", config=config, settings=settings, today=date(2026, 9, 2)
    )
    assert request.output_language == "Chinese"

    monkeypatch.setenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "English")
    request = build_default_scheduled_request(
        "AAPL",
        "stock",
        config=config,
        settings={"output_language": {"value": "Japanese", "source": "sqlite"}},
        today=date(2026, 9, 2),
    )
    assert request.output_language == "English"


def test_default_scheduled_request_ignores_invalid_language_environment(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "Klingon")
    request = build_default_scheduled_request(
        "AAPL",
        "stock",
        config={**DEFAULT_CONFIG, "output_language": "French"},
        settings={"output_language": {"value": "Japanese", "source": "sqlite"}},
        today=date(2026, 9, 2),
    )

    assert request.output_language == "Japanese"
