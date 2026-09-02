"""Pure scheduling primitives shared by the scheduler service and API."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from cli.models import AnalystType
from cli.utils import normalize_ticker_symbol
from tradingagents.default_config import DEFAULT_CONFIG

from .config import OUTPUT_LANGUAGES, market_data_catalog, model_catalog
from .models import AnalysisRequest


class CronExpressionError(ValueError):
    """Raised when a user supplies an invalid standard five-field cron."""


def _resolve_timezone(value: Any, now: datetime | None):
    if isinstance(value, str):
        try:
            return ZoneInfo(value)
        except Exception as exc:
            raise CronExpressionError(f"invalid timezone: {value}") from exc
    if value is not None:
        return value
    if now is not None and now.tzinfo is not None and now.utcoffset() is not None:
        return now.tzinfo
    return datetime.now().astimezone().tzinfo or timezone.utc


def validate_cron_expression(expression: str, *, timezone: Any = None) -> str:
    if not isinstance(expression, str):
        raise CronExpressionError("cron expression must be a string with exactly 5 fields")
    normalized = " ".join(expression.split())
    if len(normalized.split()) != 5:
        raise CronExpressionError("cron expression must contain exactly 5 fields")
    try:
        CronTrigger.from_crontab(normalized, timezone=_resolve_timezone(timezone, None))
    except (TypeError, ValueError) as exc:
        raise CronExpressionError(f"invalid cron expression: {exc}") from exc
    return normalized


def next_fire_times(
    expression: str,
    count: int = 3,
    now: datetime | None = None,
    timezone: Any = None,
) -> list[str]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("count must be a positive integer")
    selected_timezone = _resolve_timezone(timezone, now)
    normalized = validate_cron_expression(expression, timezone=selected_timezone)
    trigger = CronTrigger.from_crontab(normalized, timezone=selected_timezone)
    current = now or datetime.now(selected_timezone)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=selected_timezone)
    else:
        current = current.astimezone(selected_timezone)
    result: list[str] = []
    previous = current
    while len(result) < count:
        fire_time = trigger.get_next_fire_time(previous, current)
        if fire_time is None:
            break
        result.append(fire_time.isoformat())
        previous = fire_time
        current = fire_time
    return result


def build_default_scheduled_request(
    symbol: str,
    asset_type: str,
    config: dict[str, Any] | None = None,
    *,
    settings: dict[str, Any] | None = None,
    today: date | None = None,
) -> AnalysisRequest:
    active_config = config or DEFAULT_CONFIG
    _, defaults = model_catalog(active_config)
    quote_strategy = market_data_catalog(active_config, settings)["quote_strategy_id"]["value"]
    language_setting = (settings or {}).get("output_language")
    persisted_language = (
        language_setting.get("value") if isinstance(language_setting, dict) else None
    )
    output_language = next(
        (
            candidate
            for candidate in (
                os.getenv("TRADINGAGENTS_OUTPUT_LANGUAGE"),
                persisted_language,
                active_config.get("output_language"),
                defaults["output_language"],
            )
            if candidate in OUTPUT_LANGUAGES
        ),
        "Chinese",
    )
    return AnalysisRequest(
        ticker=symbol,
        analysis_date=today or date.today(),
        asset_type=asset_type,
        analysts=list(AnalystType),
        research_depth=1,
        output_language=output_language,
        provider=defaults["provider"],
        quick_model=defaults["quick_model"],
        deep_model=defaults["deep_model"],
        quote_strategy_id=quote_strategy,
    )


def infer_scheduled_request(
    symbol: str,
    asset_type: str,
    *,
    latest_request: AnalysisRequest | dict[str, Any] | None,
    config: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    today: date | None = None,
) -> tuple[AnalysisRequest, str]:
    if latest_request is not None:
        try:
            request = (
                latest_request.model_copy(deep=True)
                if isinstance(latest_request, AnalysisRequest)
                else AnalysisRequest.model_validate(latest_request)
            )
            refreshed = request.model_dump(mode="json")
            refreshed.update(
                {
                    "ticker": normalize_ticker_symbol(symbol),
                    "asset_type": asset_type,
                    "analysis_date": today or date.today(),
                }
            )
            return AnalysisRequest.model_validate(refreshed), "last_successful"
        except (TypeError, ValueError):
            pass
    return (
        build_default_scheduled_request(
            symbol,
            asset_type,
            config,
            settings=settings,
            today=today,
        ),
        "global_default",
    )
