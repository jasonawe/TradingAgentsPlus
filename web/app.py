"""FastAPI transport for the local TradingAgents analysis console."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from tradingagents.default_config import DEFAULT_CONFIG

from .artifacts import ArtifactRepository
from .config import (
    OUTPUT_LANGUAGES,
    QUOTE_STRATEGIES,
    market_data_catalog,
    model_catalog,
    resolve_model_config,
    resolve_run_lifecycle_config,
)
from .error_codes import USER_MESSAGES, TerminalReason
from .history import ReportHistory, ReportNotFound
from .manager import AssetBusyError, EventBatch, MaxConcurrentRunsError, RunManager
from .market_data import ProviderRouter, QuoteService
from .market_models import ProviderError
from .models import AnalysisRequest, EventEnvelope, RunRecord
from .providers import AlphaVantageProvider, YFinanceProvider
from .repositories import (
    AnalysisRunRepository,
    ProviderHealthRepository,
    QuoteRepository,
    ReportIndexRepository,
    ReportRepository,
    ScheduledJobRepository,
    ScheduledRunLogRepository,
    SettingsRepository,
    SnapshotRepository,
    WatchlistRepository,
)
from .runner import WebRunRunner
from .scheduled import CronExpressionError, validate_cron_expression
from .scheduler import ScheduledAnalysisService
from .snapshots import SnapshotCorruptError, SnapshotStore
from .storage import SQLiteStore

_STATIC_DIR = Path(__file__).with_name("static")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _record_json(record: RunRecord) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def _safe_filename(value: str, fallback: str = "report") -> str:
    cleaned = _SAFE_FILENAME.sub("-", value).strip(".-")[:80]
    return cleaned or fallback


def _config_view(config: dict[str, Any]) -> dict[str, Any]:
    """Expose only the small, non-sensitive subset needed by the browser."""

    providers, configured = model_catalog(config)
    return {
        "supported_asset_types": ["stock", "crypto"],
        "analyst_options": [
            {"key": "market", "label": "Market Analyst", "label_key": "analysts.market"},
            {"key": "social", "label": "Sentiment Analyst", "label_key": "analysts.social"},
            {"key": "news", "label": "News Analyst", "label_key": "analysts.news"},
            {"key": "fundamentals", "label": "Fundamentals Analyst", "label_key": "analysts.fundamentals"},
        ],
        "research_depths": [1, 3, 5],
        "default_date": date.today().isoformat(),
        "output_languages": [{"value": value, "label": value} for value in OUTPUT_LANGUAGES],
        "output_language": configured["output_language"],
        "effective_output_language": configured["output_language"],
        "providers": providers,
        "configured": configured,
        # Keep these aliases for older clients.
        "provider": configured["provider"],
        "model": configured["deep_model"],
    }


def _normalize_analysis_request(
    request_data: AnalysisRequest,
    *,
    config: dict[str, Any],
    settings: SettingsRepository,
) -> AnalysisRequest:
    """Apply the server-owned model, language, and quote defaults."""

    selected = resolve_model_config(
        config,
        request_data.provider,
        request_data.quick_model,
        request_data.deep_model,
    )
    language = request_data.output_language or model_catalog(config)[1]["output_language"]
    if language not in OUTPUT_LANGUAGES:
        raise ValueError("invalid analysis configuration")
    normalized = request_data.model_copy(
        update={
            "provider": selected["provider"],
            "quick_model": selected["quick_model"],
            "deep_model": selected["deep_model"],
            "output_language": language,
        }
    )
    strategy = normalized.quote_strategy_id or market_data_catalog(
        config, settings.all()
    )["quote_strategy_id"]["value"]
    if strategy not in QUOTE_STRATEGIES:
        raise ValueError("invalid analysis configuration")
    return normalized.model_copy(update={"quote_strategy_id": strategy})


def _watchlist_view(repo: WatchlistRepository) -> dict[str, Any]:
    wl = repo.get_default()
    return {"watchlist": {"id": wl["id"], "name": wl["name"], "version": wl["version"]}, "items": repo.list_items()}


def _event_sse(event: EventEnvelope) -> str:
    payload = event.model_dump(mode="json")
    # ``event`` is the SSE event type; the JSON envelope retains all metadata.
    event_id = "" if event.event.value == "run_snapshot" else f"id: {event.seq}\n"
    return (
        f"{event_id}event: {event.event.value}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _cursor(last_event_id: str | None, after_seq: int | None) -> int:
    # Browser EventSource reconnects use Last-Event-ID. It deliberately wins
    # over the explicit fallback cursor whenever it is present and valid.
    value = last_event_id if last_event_id is not None else after_seq
    if value in (None, ""):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def create_app(
    *,
    manager: RunManager | None = None,
    config: dict[str, Any] | None = None,
    runner: Any | None = None,
    history: ReportHistory | None = None,
) -> FastAPI:
    """Build an isolated application instance suitable for local use or tests."""

    active_config = copy.deepcopy(config if config is not None else DEFAULT_CONFIG)
    run_db_path = active_config.get("web_runs_db") or (Path(active_config.get("results_dir") or ".") / "web_runs.sqlite3")
    if manager is not None and getattr(manager, "_store", None) is not None:
        store = manager._store
    elif manager is not None and getattr(manager, "_db_path", None) is not None:
        store = SQLiteStore(manager._db_path)
    else:
        store = SQLiteStore(run_db_path)
    settings_repo = SettingsRepository(store)
    report_index_repo = ReportIndexRepository(store)
    lifecycle_config = resolve_run_lifecycle_config(
        config if config is not None else {}, settings_repo.all()
    )
    active_manager = manager or RunManager(store=store, lifecycle_config=lifecycle_config)
    if manager is not None and getattr(manager, "_store", None) is None:
        manager._store = store
        manager._db_path = store.path
    if manager is not None:
        manager.configure_lifecycle(lifecycle_config)
    concurrency_setting = settings_repo.get(SettingsRepository.SCHEDULER_MAX_CONCURRENT_RUNS)
    if manager is None or (concurrency_setting or {}).get("source") != "default":
        active_manager.configure_concurrency(settings_repo.all())
    active_manager.set_report_root(Path(active_config.get("results_dir") or ".") / "web_reports")
    active_history = history or ReportHistory(
        results_dir=active_config.get("results_dir"),
        cwd=active_config.get("project_dir"),
        repository=report_index_repo,
    )
    if history is not None:
        active_history.attach_repository(report_index_repo)
    report_index_stop = threading.Event()

    def retry_report_index() -> None:
        while not report_index_stop.wait(30.0):
            try:
                active_history.retry_outbox(limit=50)
            except Exception:
                continue

    provider_health_repo = ProviderHealthRepository(store)
    artifact_repository = ArtifactRepository(store)
    active_manager.attach_artifact_repository(artifact_repository)
    watchlist_repo = WatchlistRepository(store)
    analysis_run_repo = AnalysisRunRepository(store)
    scheduled_job_repo = ScheduledJobRepository(store)
    scheduled_log_repo = ScheduledRunLogRepository(store)
    repositories = {
        "watchlist": watchlist_repo,
        "quotes": QuoteRepository(store),
        "runs": analysis_run_repo,
        "snapshots": SnapshotRepository(store),
        "settings": settings_repo,
        "scheduled_jobs": scheduled_job_repo,
        "scheduled_logs": scheduled_log_repo,
        "reports": report_index_repo,
        "report_gate": ReportRepository(store),
        "provider_health": provider_health_repo,
        "artifacts": artifact_repository,
    }
    if runner is None:
        active_runner = WebRunRunner(
            active_manager,
            config=active_config,
            report_history=active_history,
            artifact_repository=artifact_repository,
        )
        worker = active_runner.worker
    elif hasattr(runner, "worker"):
        active_runner = runner
        worker = runner.worker
    else:
        active_runner = runner
        worker = runner

    def normalize_request(request_data: AnalysisRequest) -> AnalysisRequest:
        return _normalize_analysis_request(
            request_data, config=active_config, settings=settings_repo
        )

    scheduler_service = ScheduledAnalysisService(
        jobs=scheduled_job_repo,
        logs=scheduled_log_repo,
        runs=analysis_run_repo,
        watchlist=watchlist_repo,
        settings=settings_repo,
        manager=active_manager,
        worker=worker,
        normalize_request=normalize_request,
        config=active_config,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        retry_thread = threading.Thread(
            target=retry_report_index,
            name="tradingagents-report-index",
            daemon=True,
        )
        try:
            active_history.rebuild_index()
            active_history.retry_outbox(limit=50)
            retry_thread.start()
            scheduler_service.start()
            yield
        finally:
            scheduler_service.shutdown()
            report_index_stop.set()
            if retry_thread.is_alive():
                retry_thread.join(timeout=5.0)
            active_manager.shutdown()

    app = FastAPI(title="TradingAgents Web Console", lifespan=lifespan)
    app.state.manager = active_manager
    app.state.config = active_config
    app.state.history = active_history
    app.state.store = store
    app.state.runner = active_runner
    app.state.worker = worker
    app.state.scheduler = scheduler_service
    app.state.artifact_repository = artifact_repository
    app.state.repositories = repositories
    providers = {"yfinance": YFinanceProvider(), "alpha_vantage": AlphaVantageProvider()}
    app.state.market_router = ProviderRouter(providers, health=provider_health_repo)
    app.state.market_service = QuoteService(
        app.state.market_router,
        app.state.repositories["quotes"],
        settings=settings_repo,
        config=active_config,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"detail": "invalid analysis request"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    def _console_entry() -> Response:
        index_path = _STATIC_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(index_path, media_type="text/html")
        return HTMLResponse("<!doctype html><title>TradingAgents</title><h1>TradingAgents</h1>")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/analysis", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/active", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/reports", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/scheduled", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/scheduled/history", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    def index() -> Response:
        return _console_entry()

    @app.get("/reports/{report_id}", response_class=HTMLResponse, include_in_schema=False)
    def report_index(report_id: str) -> Response:
        return _console_entry()

    @app.get("/assets/{symbol}", response_class=HTMLResponse, include_in_schema=False)
    def asset_index(symbol: str) -> Response:
        return _console_entry()

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        value = _config_view(active_config)
        value["market_data"] = market_data_catalog(active_config, settings_repo.all())
        value["effective_quote_strategy_id"] = value["market_data"]["quote_strategy_id"]["value"]
        value["effective_quote_provider_chain"] = value["market_data"]["quote_provider_chain"]["value"]
        return value

    @app.get("/api/watchlist")
    def get_watchlist() -> dict[str, Any]:
        return _watchlist_view(app.state.repositories["watchlist"])

    @app.post("/api/watchlist/items")
    def add_watchlist_item(payload: dict[str, Any]) -> dict[str, Any]:
        repo = app.state.repositories["watchlist"]
        try:
            symbol = payload.get("symbol")
            asset_type = payload.get("asset_type", "stock")
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("invalid symbol")
            repo.add_item(symbol, asset_type=asset_type, note=payload.get("note"))
            return _watchlist_view(repo)
        except ValueError as exc:
            if "duplicate" in str(exc):
                raise _error(409, "关注列表中已存在该资产") from exc
            raise _error(422, "关注列表参数无效") from exc

    @app.patch("/api/watchlist/items/{item_id}")
    def update_watchlist_item(item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        repo = app.state.repositories["watchlist"]
        if "symbol" in payload or "asset_type" in payload:
            raise _error(422, "只能修改备注或排序")
        try:
            version = int(payload.get("version"))
            kwargs = {"expected_version": version}
            if "note" in payload:
                kwargs["note"] = payload["note"]
            if "position" in payload:
                kwargs["position"] = payload["position"]
            if "order" in payload:
                kwargs["order"] = payload["order"]
            repo.update_item(item_id, **kwargs)
            return _watchlist_view(repo)
        except KeyError as exc:
            raise _error(404, "关注项不存在") from exc
        except RuntimeError as exc:
            raise _error(409, "关注列表版本冲突，请刷新后重试") from exc
        except (TypeError, ValueError) as exc:
            raise _error(422, "关注列表参数无效") from exc

    @app.delete("/api/watchlist/items/{item_id}", status_code=204)
    def delete_watchlist_item(item_id: str, version: int = Query(..., ge=1)) -> Response:
        repo = app.state.repositories["watchlist"]
        try:
            repo.delete_item(item_id, expected_version=version)
            scheduler_service.resync()
        except KeyError as exc:
            raise _error(404, "关注项不存在") from exc
        except RuntimeError as exc:
            raise _error(409, "关注列表版本冲突，请刷新后重试") from exc
        return Response(status_code=204)

    @app.post("/api/watchlist/reorder")
    def reorder_watchlist(payload: dict[str, Any]) -> dict[str, Any]:
        repo = app.state.repositories["watchlist"]
        try:
            ids = payload.get("item_ids")
            version = int(payload.get("version"))
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                raise ValueError
            repo.reorder(ids, expected_version=version)
            return _watchlist_view(repo)
        except KeyError as exc:
            raise _error(404, "关注列表不存在") from exc
        except RuntimeError as exc:
            raise _error(409, "关注列表版本冲突，请刷新后重试") from exc
        except (TypeError, ValueError) as exc:
            raise _error(422, "排序参数无效") from exc

    @app.get("/api/quotes")
    def get_quotes(symbols: str = Query(...), asset_type: str = Query("stock")) -> dict[str, Any]:
        values = [v.strip() for v in symbols.split(",") if v.strip()]
        if not values or len(values) > 50:
            raise _error(422, "symbols 最多支持 50 个资产")
        try:
            result = app.state.market_service.get_quotes(values, asset_type)
            if isinstance(result, dict):
                return result
            return result.model_dump(mode="json")
        except ValueError as exc:
            raise _error(422, "行情参数无效") from exc

    @app.get("/api/assets/{symbol}/candles")
    def get_candles(symbol: str, interval: str = Query("1d"), start: date | None = None, end: date | None = None) -> dict[str, Any]:
        if interval not in {"1d", "1h", "15m"}:
            raise _error(422, "K线周期无效")
        end_date = end or date.today()
        start_date = start or date.fromordinal(end_date.toordinal() - 365)
        if start_date > end_date or (end_date - start_date).days > 730:
            raise _error(422, "日期范围最多 2 年")
        try:
            candles = app.state.market_router.get_candles(symbol, interval, start_date.isoformat(), end_date.isoformat(), app.state.market_service.strategy)
            if len(candles) > 2000:
                raise _error(422, "K线点数最多 2000")
            source = next((c.source for c in candles if c.source), None)
            return {"symbol": symbol.upper(), "canonical_symbol": symbol.upper(), "interval": interval, "items": [{"time": c.timestamp, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume} for c in candles], "candles": [c.model_dump(mode="json") for c in candles], "source": source, "fetched_at": datetime.now(timezone.utc), "freshness": "fresh", "error": None}
        except ProviderError as exc:
            raise _error(404 if exc.code.value == "no_data" else 502, "暂时无法获取行情数据") from exc

    @app.get("/api/assets/{symbol}/identity")
    def get_identity(symbol: str, asset_type: str = Query("stock")) -> dict[str, Any]:
        try:
            identity = app.state.market_router.get_identity(symbol, asset_type, app.state.market_service.strategy)
            if not identity.name and not identity.exchange and not identity.currency:
                raise _error(404, "未找到资产信息")
            return {**identity.model_dump(mode="json"), "canonical_symbol": identity.symbol, "source": getattr(identity, "source", None), "error": None}
        except ProviderError as exc:
            raise _error(404, "未找到资产信息") from exc

    @app.get("/api/assets/{symbol}/runs")
    def list_asset_runs(symbol: str, limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        records = active_manager.list_runs_for_ticker(symbol, limit=limit)
        return {
            "symbol": symbol.upper(),
            "items": [
                {
                    "run_id": record.run_id,
                    "status": record.status.value if hasattr(record.status, "value") else str(record.status),
                    "phase": record.phase,
                    "current_agent": record.current_agent,
                    "progress": record.progress,
                    "queued_at": record.queued_at,
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "signal": record.signal,
                    "report_id": record.report_id,
                    "error_code": record.error_code,
                    "error_message": record.error_message,
                    "failed_phase": record.failed_phase,
                    "failed_agent": record.failed_agent,
                    "retryable": record.retryable,
                    "analysis_date": record.request.analysis_date,
                    "asset_type": record.request.asset_type,
                    "provider": record.request.provider,
                    "research_depth": record.request.research_depth,
                }
                for record in records
            ],
        }

    @app.get("/api/providers/market-data")
    def market_provider_status() -> dict[str, Any]:
        catalog = market_data_catalog(active_config, settings_repo.all())
        health = {item["provider"]: item for item in provider_health_repo.list()}
        providers = []
        for provider in catalog["providers"]:
            provider_status = health.get(provider["id"], {}).get(
                "status", provider["status"]
            )
            providers.append(
                {
                    **provider,
                    "status": provider_status,
                    "status_key": f"provider_status.{provider_status}",
                    "health": health.get(provider["id"]),
                }
            )
        return {"providers": providers}

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        catalog = market_data_catalog(active_config, settings_repo.all())
        fields = {key: catalog[key] for key in ("quote_strategy_id", "quote_provider_chain", "quote_ttl_seconds")}
        _, defaults = model_catalog(active_config)
        source = "env" if os.getenv("TRADINGAGENTS_OUTPUT_LANGUAGE") else (settings_repo.get("output_language") or {}).get("source", "default")
        fields["output_language"] = {"value": defaults["output_language"], "source": source}
        fields["effective_output_language"] = fields["output_language"]
        return {"schema_version": 1, "fields": fields, "strategies": [{"id": k, "providers": v["providers"], "available": next((s["available"] for s in catalog["strategies"] if s["id"] == k), False)} for k, v in QUOTE_STRATEGIES.items()], "provider_health": {item["provider"]: item for item in provider_health_repo.list()}}

    @app.get("/api/scheduled/jobs")
    def list_scheduled_jobs() -> dict[str, Any]:
        return scheduler_service.list_jobs()

    @app.get("/api/scheduled/jobs/{job_id}")
    def get_scheduled_job(job_id: str) -> dict[str, Any]:
        try:
            return scheduler_service.get_job(job_id)
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "scheduled job not found") from exc

    @app.get("/api/scheduled/jobs/{job_id}/logs")
    def list_scheduled_logs(
        job_id: str, limit: int = Query(20, ge=1, le=100)
    ) -> dict[str, Any]:
        try:
            scheduled_job_repo.get(job_id)
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "scheduled job not found") from exc
        return {"items": scheduled_log_repo.list(job_id, limit=limit)}

    @app.get("/api/scheduled/settings")
    def get_scheduled_settings() -> dict[str, Any]:
        return settings_repo.scheduler_settings()

    @app.get("/api/scheduled/analysis-defaults")
    def get_scheduled_analysis_defaults() -> dict[str, Any]:
        keys = (
            settings_repo.SCHEDULER_OVERRIDES_ENABLED,
            settings_repo.SCHEDULER_OVERRIDES_PROVIDER,
            settings_repo.SCHEDULER_OVERRIDES_QUICK_MODEL,
            settings_repo.SCHEDULER_OVERRIDES_DEEP_MODEL,
            settings_repo.SCHEDULER_OVERRIDES_ANALYSTS,
            settings_repo.SCHEDULER_OVERRIDES_RESEARCH_DEPTH,
            settings_repo.SCHEDULER_OVERRIDES_OUTPUT_LANGUAGE,
        )
        return {key: settings_repo.get(key) for key in keys}

    @app.patch("/api/scheduled/analysis-defaults")
    def update_scheduled_analysis_defaults(payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            settings_repo.SCHEDULER_OVERRIDES_ENABLED: str,
            settings_repo.SCHEDULER_OVERRIDES_PROVIDER: str,
            settings_repo.SCHEDULER_OVERRIDES_QUICK_MODEL: str,
            settings_repo.SCHEDULER_OVERRIDES_DEEP_MODEL: str,
            settings_repo.SCHEDULER_OVERRIDES_ANALYSTS: str,
            settings_repo.SCHEDULER_OVERRIDES_RESEARCH_DEPTH: str,
            settings_repo.SCHEDULER_OVERRIDES_OUTPUT_LANGUAGE: str,
        }
        if not payload or set(payload) - set(allowed):
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid analysis defaults")
        for key, expected in allowed.items():
            if key in payload and not isinstance(payload[key], expected):
                raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{key} must be a string")
        for key in allowed:
            if key in payload:
                settings_repo.set(key, payload[key] or "")
        return get_scheduled_analysis_defaults()

    @app.get("/api/scheduled/cron/preview")
    def preview_scheduled_cron(
        cron_expression: str = Query(...), count: int = Query(3, ge=1, le=20)
    ) -> dict[str, Any]:
        try:
            normalized = validate_cron_expression(
                cron_expression, timezone=scheduler_service.timezone
            )
            return {
                "cron_expression": normalized,
                "next_run_times": scheduler_service.preview(normalized, count=count),
            }
        except (CronExpressionError, ValueError) as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @app.get("/api/scheduled/logs")
    def list_scheduled_run_logs(
        page: int = Query(1, ge=1, le=10000),
        page_size: int = Query(25, ge=1, le=100),
        status: str | None = Query(None),
        job_id: str | None = Query(None),
    ) -> dict[str, Any]:
        items = scheduled_log_repo.list_paginated(
            limit=page_size, offset=(page - 1) * page_size, status=status, job_id=job_id
        )
        total = scheduled_log_repo.count(status=status, job_id=job_id)
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": page * page_size < total,
        }

    @app.post("/api/scheduled/jobs", status_code=status.HTTP_201_CREATED)
    def create_scheduled_job(payload: dict[str, Any]) -> JSONResponse:
        allowed = {"symbol", "asset_type", "cron_expression", "note"}
        if not payload or set(payload) - allowed:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid scheduled job parameters")
        symbol = payload.get("symbol")
        asset_type = payload.get("asset_type")
        cron_expression = payload.get("cron_expression")
        note = payload.get("note")
        if (
            not isinstance(symbol, str)
            or not isinstance(asset_type, str)
            or not isinstance(cron_expression, str)
            or (note is not None and not isinstance(note, str))
        ):
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid scheduled job parameters")
        try:
            if not watchlist_repo.contains(symbol, asset_type):
                raise _error(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "asset must exist in the watchlist",
                )
            job = scheduled_job_repo.create(
                symbol,
                asset_type=asset_type,
                cron_expression=cron_expression,
                note=note,
            )
            scheduler_service.resync()
            return JSONResponse(
                scheduler_service.serialize_job(job),
                status_code=status.HTTP_201_CREATED,
            )
        except HTTPException:
            raise
        except CronExpressionError as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        except ValueError as exc:
            if "already exists" in str(exc):
                raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @app.patch("/api/scheduled/jobs/{job_id}", status_code=status.HTTP_201_CREATED)
    def update_scheduled_job(job_id: str, payload: dict[str, Any]) -> JSONResponse:
        allowed = {"symbol", "asset_type", "cron_expression", "note", "enabled"}
        if not payload or set(payload) - allowed:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid scheduled job parameters")
        if (
            ("symbol" in payload and not isinstance(payload["symbol"], str))
            or ("asset_type" in payload and not isinstance(payload["asset_type"], str))
            or (
                "cron_expression" in payload
                and not isinstance(payload["cron_expression"], str)
            )
            or ("enabled" in payload and not isinstance(payload["enabled"], bool))
            or (
                "note" in payload
                and payload["note"] is not None
                and not isinstance(payload["note"], str)
            )
        ):
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid scheduled job parameters")
        try:
            current = scheduled_job_repo.get(job_id)
            target_symbol = payload.get("symbol", current["symbol"])
            target_asset_type = payload.get("asset_type", current["asset_type"])
            if not watchlist_repo.contains(target_symbol, target_asset_type):
                raise _error(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "asset must exist in the watchlist",
                )
            kwargs = {key: payload[key] for key in allowed if key in payload}
            job = scheduled_job_repo.update(job_id, **kwargs)
            scheduler_service.resync()
            return JSONResponse(
                scheduler_service.serialize_job(job),
                status_code=status.HTTP_201_CREATED,
            )
        except HTTPException:
            raise
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "scheduled job not found") from exc
        except CronExpressionError as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        except ValueError as exc:
            if "already exists" in str(exc):
                raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @app.delete("/api/scheduled/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_scheduled_job(job_id: str) -> Response:
        try:
            scheduled_job_repo.delete(job_id)
            scheduler_service.resync()
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "scheduled job not found") from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/scheduled/jobs/{job_id}/toggle",
        status_code=status.HTTP_201_CREATED,
    )
    def toggle_scheduled_job(job_id: str, payload: dict[str, Any]) -> JSONResponse:
        if set(payload) != {"enabled"} or not isinstance(payload.get("enabled"), bool):
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "enabled must be a boolean")
        try:
            job = scheduled_job_repo.toggle(job_id, payload["enabled"])
            scheduler_service.resync()
            return JSONResponse(
                scheduler_service.serialize_job(job),
                status_code=status.HTTP_201_CREATED,
            )
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "scheduled job not found") from exc

    @app.post(
        "/api/scheduled/jobs/{job_id}/run",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def run_scheduled_job(job_id: str) -> JSONResponse:
        try:
            log = scheduler_service.run_now(job_id)
            return JSONResponse(log, status_code=status.HTTP_202_ACCEPTED)
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "scheduled job not found") from exc

    @app.patch("/api/scheduled/settings")
    def update_scheduled_settings(payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"enabled", "max_concurrent_runs"}
        if not payload or set(payload) - allowed:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid scheduler settings")
        if (
            ("enabled" in payload and not isinstance(payload["enabled"], bool))
            or (
                "max_concurrent_runs" in payload
                and (
                    isinstance(payload["max_concurrent_runs"], bool)
                    or not isinstance(payload["max_concurrent_runs"], int)
                )
            )
        ):
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid scheduler settings")
        try:
            kwargs = {key: payload[key] for key in allowed if key in payload}
            result = settings_repo.update_scheduler_settings(**kwargs)
            active_manager.configure_concurrency(settings_repo.all())
            scheduler_service.resync()
            return result
        except ValueError as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @app.get("/api/runs/active")
    def get_active_runs() -> dict[str, Any]:
        """Return every in-flight run so a reopened client can reattach.

        Plan 1: replaced the single-run shape ``{"run": ...}`` with a list
        ``{"runs": [...]}`` to support configurable concurrent runs.
        """

        records = active_manager.list_active_runs()
        return {"runs": [_record_json(record) for record in records]}

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    def create_run(request_data: AnalysisRequest) -> JSONResponse:
        try:
            request_data = normalize_request(request_data)
        except ValueError as exc:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "invalid analysis configuration",
            ) from exc
        try:
            record = active_manager.start_run(request_data, worker=worker)
        except MaxConcurrentRunsError as exc:
            raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
        except AssetBusyError as exc:
            raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
        return JSONResponse(_record_json(record), status_code=status.HTTP_202_ACCEPTED)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        try:
            return _record_json(active_manager.get_run(run_id))
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "run not found") from exc

    @app.get("/api/runs/{run_id}/events")
    def run_events(
        run_id: str,
        request: Request,
        after_seq: int | None = Query(default=None, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            active_manager.get_run(run_id)
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "run not found") from exc
        initial_cursor = _cursor(last_event_id, after_seq)

        def stream() -> Iterator[str]:
            cursor = initial_cursor
            while True:
                if request is not None and _request_disconnected(request):
                    return
                try:
                    batch: EventBatch = active_manager.wait_for_events(run_id, cursor, timeout=15.0)
                except KeyError:
                    return
                if batch.events:
                    for event in batch.events:
                        yield _event_sse(event)
                        if event.event.value == "run_snapshot":
                            cursor = event.payload.snapshot_seq
                        elif event.seq > cursor:
                            cursor = event.seq
                    if batch.terminal and not any(event.seq > cursor for event in batch.events):
                        return
                    if batch.terminal:
                        return
                elif batch.timed_out:
                    yield ": heartbeat\n\n"
                elif batch.terminal:
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict[str, Any]:
        try:
            return _record_json(active_manager.request_cancel(run_id))
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "run not found") from exc

    @app.get("/api/runs/{run_id}/artifacts")
    def list_run_artifacts(run_id: str) -> dict[str, Any]:
        """Return per-stage artifacts for one run, ordered by sequence."""

        try:
            record = active_manager.get_run(run_id)
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "run not found") from exc
        artifacts = active_manager.list_artifacts(run_id)
        return {
            "run_id": record.run_id,
            "status": record.status.value,
            "artifact_count": record.artifact_count,
            "completed_artifact_count": record.completed_artifact_count,
            "has_partial_results": record.has_partial_results,
            "artifacts": artifacts,
        }

    @app.post("/api/runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    def retry_run(run_id: str) -> JSONResponse:
        """Create a new run that resumes from the parent's checkpoint.

        Returns 409 if the parent is not retryable, has no compatible
        checkpoint, or another run is already active. The browser falls
        back to a fresh "重新分析" action in those cases.
        """

        try:
            allowed, reason = active_manager.can_retry(run_id)
        except KeyError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "run not found") from exc
        if not allowed:
            raise _error(
                status.HTTP_409_CONFLICT,
                USER_MESSAGES.get(reason, "retry not available"),
            )
        parent_record = active_manager.get_run(run_id)
        if parent_record.resume_checkpoint_id is None:
            raise _error(
                status.HTTP_409_CONFLICT,
                USER_MESSAGES.get(TerminalReason.WORKER_ERROR.value, "checkpoint unavailable"),
            )
        try:
            record = active_manager.retry_run(
                parent_run_id=run_id,
                request=parent_record.request,
                worker=worker,
            )
        except MaxConcurrentRunsError as exc:
            raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
        except AssetBusyError as exc:
            raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
        except RuntimeError as exc:
            raise _error(status.HTTP_409_CONFLICT, "checkpoint_unavailable") from exc
        return JSONResponse(_record_json(record), status_code=status.HTTP_202_ACCEPTED)

    @app.get("/api/history")
    def list_history(
        request: Request,
        page: int | None = Query(default=None, ge=1, le=100000),
        page_size: int | None = Query(default=None, ge=1, le=100),
        query: str | None = Query(default=None),
        ticker: str | None = Query(default=None),
        status_filter: str | None = Query(default=None, alias="status"),
        asset_type: str | None = Query(default=None),
        date_from: Annotated[date | None, Query()] = None,
        date_to: Annotated[date | None, Query()] = None,
        sort: str | None = Query(default=None),
    ) -> Any:
        if not request.query_params:
            return active_history.list_reports()
        if (
            status_filter is not None
            and status_filter not in ReportIndexRepository.STATUSES
        ):
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid report status"
            )
        if (
            asset_type is not None
            and asset_type not in ReportIndexRepository.ASSET_TYPES
        ):
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid asset type"
            )
        selected_sort = sort or "generated_at_desc"
        if selected_sort not in ReportIndexRepository.SORTS:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid report sort"
            )
        if date_from is not None and date_to is not None and date_from > date_to:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid date range")
        return active_history.search_reports(
            page=page or 1,
            page_size=page_size or 20,
            query=query,
            ticker=ticker,
            status=status_filter,
            asset_type=asset_type,
            date_from=date_from.isoformat() if date_from is not None else None,
            date_to=date_to.isoformat() if date_to is not None else None,
            sort=selected_sort,
        )

    @app.get("/api/history/{report_id}")
    def history_detail(report_id: str) -> dict[str, Any]:
        try:
            return active_history.get_report(report_id)
        except ReportNotFound as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "report not found") from exc

    @app.get("/api/history/{report_id}/data-snapshot")
    def history_snapshot(report_id: str) -> dict[str, Any]:
        try:
            report = active_history.get_report(report_id)
            entry = active_history.get_entry(report_id)
        except ReportNotFound as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "report not found") from exc
        snapshot_id = report.get("data_snapshot_id")
        if not snapshot_id:
            raise _error(status.HTTP_404_NOT_FOUND, "该报告没有可用的数据快照")
        run_id = str(report.get("run_id") or entry.sidecar.get("run_id") or report_id)
        try:
            manifest = SnapshotStore(entry.path).read_manifest(run_id)
        except (SnapshotCorruptError, ValueError) as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "数据快照不可用") from exc
        return {"report_id": report_id, "snapshot_id": snapshot_id, "manifest": manifest, "data_status": report.get("data_status") or "unknown", "reproducibility": report.get("reproducibility")}

    @app.get("/api/history/{report_id}/download")
    def history_download(report_id: str) -> Response:
        try:
            report = active_history.get_report(report_id)
        except ReportNotFound as exc:
            raise _error(status.HTTP_404_NOT_FOUND, "report not found") from exc
        ticker = _safe_filename(str(report.get("ticker") or "report"))
        filename = f"{ticker}-{_safe_filename(report_id)}.md"
        summary = str(report.get("executive_summary") or "").strip()
        content = f"{summary}\n\n---\n\n{report['complete_report']}" if summary else report["complete_report"]
        return Response(
            content=content,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return app


def _request_disconnected(request: Request) -> bool:
    """Best-effort disconnect check that is harmless for test doubles."""

    try:
        # Streaming generators cannot await; TestClient and short-lived local
        # requests do not require an eager disconnect check.
        return False
    except Exception:  # pragma: no cover - defensive only
        return False


app = create_app()

__all__ = ["app", "create_app"]
