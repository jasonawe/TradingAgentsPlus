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

from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from tradingagents.default_config import DEFAULT_CONFIG

from cli.utils import is_valid_ticker_input, normalize_ticker_symbol

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
from .providers import AKShareProvider, AlphaVantageProvider, EastMoneyProvider, YFinanceProvider
import logging

# Application loggers (web.*) inherit the root logger; without a configured
# root handler, INFO messages from app code are silently dropped while
# uvicorn.log is fine because uvicorn attaches its own handlers. Configure
# the root logger once so background workers (alert_monitor, quote_prewarmer,
# scheduler, ...) actually emit their startup / loop messages.
if not logging.getHandlerByName("tradingagents-app"):
    _app_handler = logging.StreamHandler()
    _app_handler.name = "tradingagents-app"
    _app_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(_app_handler)
    if logging.getLogger().level >= logging.INFO:
        logging.getLogger().setLevel(logging.INFO)

LOGGER = logging.getLogger(__name__)

from .alert_engine import AlertEngine
from .notifier import Notifier
from .alert_monitor import AlertMonitor
from .quote_prewarmer import QuotePrewarmer
from .repositories import (
    AlertRepository,
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
    NoteRepository,
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
        "notes": NoteRepository(store),
        "alerts": AlertRepository(store),
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

    # Stage C: 把 notes / alerts repo 注入 tools_bridge,write tools 才能用
    try:
        from tradingagents.agents.general.tools_bridge import set_repositories
        set_repositories({
            "notes": repositories["notes"],
            "alerts": repositories["alerts"],
        })
        LOGGER.info("Stage C: repositories injected for write tools")
    except ImportError:
        pass
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
            alert_monitor = getattr(app.state, "alert_monitor", None)
            if alert_monitor is not None:
                alert_monitor.start()
            prewarmer = getattr(app.state, "quote_prewarmer", None)
            if prewarmer is not None:
                prewarmer.start()
            yield
        finally:
            prewarmer = getattr(app.state, "quote_prewarmer", None)
            if prewarmer is not None:
                prewarmer.stop()
            monitor = getattr(app.state, "alert_monitor", None)
            if monitor is not None:
                monitor.stop()
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
    providers = {"yfinance": YFinanceProvider(), "alpha_vantage": AlphaVantageProvider(), "eastmoney": EastMoneyProvider(), "akshare": AKShareProvider()}
    app.state.market_router = ProviderRouter(providers, health=provider_health_repo)
    app.state.market_service = QuoteService(
        app.state.market_router,
        app.state.repositories["quotes"],
        settings=settings_repo,
        config=active_config,
    )
    app.state.alert_engine = AlertEngine(app.state.repositories["alerts"])
    app.state.notifier = Notifier(settings_repo=app.state.repositories["settings"])
    app.state.alert_monitor = AlertMonitor(
        settings_repo=app.state.repositories["settings"],
        alerts_repo=app.state.repositories["alerts"],
        quote_service=app.state.market_service,
        alert_engine=app.state.alert_engine,
        notifier=app.state.notifier,
    )
    app.state.quote_prewarmer = QuotePrewarmer(
        settings_repo=app.state.repositories["settings"],
        watchlist_repo=app.state.repositories["watchlist"],
        alerts_repo=app.state.repositories["alerts"],
        quote_service=app.state.market_service,
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
        headers = {"Cache-Control": "no-store, must-revalidate"}
        if index_path.is_file():
            return FileResponse(index_path, media_type="text/html", headers=headers)
        return HTMLResponse(
            "<!doctype html><title>TradingAgents</title><h1>TradingAgents</h1>",
            headers=headers,
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/analysis", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/active", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/reports", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/scheduled", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/scheduled/history", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/alerts", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/notes", response_class=HTMLResponse, include_in_schema=False)
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

    @app.get("/api/notes")
    def list_notes(
        symbol: str | None = Query(None),
        asset_type: str = Query("stock"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """List notes. Pass ?symbol=X for one asset; omit symbol for all notes."""
        repo = app.state.repositories["notes"]
        if symbol:
            canonical = normalize_ticker_symbol(symbol)
            if not canonical or not is_valid_ticker_input(canonical):
                raise _error(422, "笔记参数无效")
            if asset_type not in {"stock", "crypto"}:
                raise _error(422, "资产类型无效")
            items = repo.list_for(canonical, asset_type)
            total = len(items)
        else:
            items, total = repo.list_all(limit=limit, offset=offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.post("/api/notes", status_code=201)
    def create_note(payload: dict[str, Any]) -> dict[str, Any]:
        raw_symbol = payload.get("symbol")
        asset_type = payload.get("asset_type", "stock")
        body = payload.get("body_md")
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            raise _error(422, "笔记参数无效")
        if not isinstance(body, str):
            raise _error(422, "笔记内容必填")
        canonical = normalize_ticker_symbol(raw_symbol)
        if not canonical or not is_valid_ticker_input(canonical):
            raise _error(422, "笔记参数无效")
        if asset_type not in {"stock", "crypto"}:
            raise _error(422, "资产类型无效")
        try:
            note = app.state.repositories["notes"].create(
                canonical, body, asset_type=asset_type
            )
        except ValueError as exc:
            raise _error(422, str(exc)) from exc
        return note

    @app.patch("/api/notes/{note_id}")
    def update_note(note_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = payload.get("body_md")
        if not isinstance(body, str):
            raise _error(422, "笔记内容必填")
        try:
            note = app.state.repositories["notes"].update(note_id, body)
        except KeyError as exc:
            raise _error(404, "笔记不存在") from exc
        except ValueError as exc:
            raise _error(422, str(exc)) from exc
        return note

    @app.delete("/api/notes/{note_id}", status_code=204)
    def delete_note(note_id: str) -> Response:
        try:
            app.state.repositories["notes"].soft_delete(note_id)
        except KeyError as exc:
            raise _error(404, "笔记不存在") from exc
        return Response(status_code=204)

    @app.post("/api/notes/{note_id}/restore")
    def restore_note(note_id: str) -> dict[str, Any]:
        try:
            return app.state.repositories["notes"].restore(note_id)
        except KeyError as exc:
            raise _error(404, "笔记不存在") from exc

    @app.get("/api/quotes")
    def get_quotes(symbols: str = Query(...), asset_type: str = Query("stock")) -> dict[str, Any]:
        values = [v.strip() for v in symbols.split(",") if v.strip()]
        if not values or len(values) > 50:
            raise _error(422, "symbols 最多支持 50 个资产")
        try:
            result = app.state.market_service.get_quotes(values, asset_type)
            payload = result if isinstance(result, dict) else result.model_dump(mode="json")
        except ValueError as exc:
            raise _error(422, "行情参数无效") from exc
        try:
            triggers = _evaluate_alerts_for_response(app, values, asset_type, payload)
        except Exception:
            triggers = []
        if triggers:
            payload["alert_triggers"] = triggers
        return payload

    def _evaluate_alerts_for_response(app, symbols: list[str], asset_type: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        engine: AlertEngine | None = getattr(app.state, "alert_engine", None)
        if engine is None:
            return []
        items = {item.get("symbol"): item for item in (payload.get("items") or []) if isinstance(item, dict)}
        triggers: list[dict[str, Any]] = []
        for symbol in symbols:
            quote = items.get(symbol) or items.get(symbol.upper())
            if not quote:
                continue
            triggers.extend(_triggers_to_payload(engine, symbol, asset_type, quote))
        return triggers

    def _triggers_to_payload(engine: AlertEngine, symbol: str, asset_type: str, quote: dict[str, Any]) -> list[dict[str, Any]]:
        raw_triggers = engine.evaluate_quote(symbol, asset_type, quote)
        events = engine.record_triggers(raw_triggers)
        notifier = getattr(app.state, "notifier", None)
        for trigger, event in zip(raw_triggers, events):
            if notifier is not None and event:
                try:
                    notifier.notify_trigger(
                        {
                            "alert_id": trigger.alert_id,
                            "symbol": trigger.symbol,
                            "asset_type": trigger.asset_type,
                            "kind": trigger.kind,
                            "message": trigger.message,
                            "snapshot": trigger.snapshot,
                        },
                        event,
                    )
                except Exception:
                    LOGGER.exception("notifier dispatch failed")
        return [
            {
                "alert_id": trigger.alert_id,
                "symbol": trigger.symbol,
                "asset_type": trigger.asset_type,
                "kind": trigger.kind,
                "message": trigger.message,
                "snapshot": trigger.snapshot,
                "event": event,
            }
            for trigger, event in zip(raw_triggers, events)
        ]

    @app.get("/api/market/kline")
    def get_kline(
        symbol: str = Query(..., description="ticker, 如 600036.SS / AAPL"),
        period: str = Query("1d", description="1d / 1w / 1M / 1m / 5m / 15m / 30m / 60m"),
        count: int = Query(240, ge=30, le=1000),
    ) -> dict[str, Any]:
        """返回 K 线 OHLCV + 成交量 + MA20/60,TradingView Lightweight Charts 格式。"""
        from web.bar_generator import build_kline_response

        try:
            return build_kline_response(symbol, period, count)
        except ValueError as exc:
            raise _error(422, str(exc)) from exc

    @app.get("/api/alerts")
    def list_alerts(
        symbol: str | None = Query(None),
        asset_type: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        repo = app.state.repositories["alerts"]
        if symbol:
            items = repo.list_for_symbol(symbol, asset_type or "stock")
            total = len(items)
        else:
            items, total = repo.list_all(limit=limit, offset=offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.post("/api/alerts", status_code=201)
    def create_alert(payload: dict[str, Any]) -> dict[str, Any]:
        repo = app.state.repositories["alerts"]
        try:
            symbol = payload.get("symbol")
            asset_type = payload.get("asset_type", "stock")
            kind = payload.get("kind")
            params = payload.get("params") or {}
            cooldown = int(payload.get("cooldown_seconds", 3600))
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("symbol 必填")
            alert = repo.create(
                symbol=symbol,
                asset_type=asset_type,
                kind=kind,
                params=params,
                cooldown_seconds=cooldown,
            )
            return alert
        except ValueError as exc:
            raise _error(422, str(exc)) from exc

    @app.patch("/api/alerts/{alert_id}")
    def update_alert(alert_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        repo = app.state.repositories["alerts"]
        try:
            return repo.update(alert_id, **payload)
        except KeyError as exc:
            raise _error(404, "告警不存在") from exc
        except ValueError as exc:
            raise _error(422, str(exc)) from exc

    @app.delete("/api/alerts/{alert_id}", status_code=204)
    def delete_alert(alert_id: str) -> Response:
        repo = app.state.repositories["alerts"]
        try:
            repo.soft_delete(alert_id)
        except KeyError as exc:
            raise _error(404, "告警不存在") from exc
        return Response(status_code=204)

    @app.get("/api/alerts/events")
    def list_alert_events(
        symbol: str | None = Query(None),
        asset_type: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        unacknowledged_only: bool = Query(False),
    ) -> dict[str, Any]:
        repo = app.state.repositories["alerts"]
        items = repo.list_events(
            symbol=symbol,
            asset_type=asset_type,
            limit=limit,
            unacknowledged_only=unacknowledged_only,
        )
        unread = repo.count_unacknowledged()
        return {"items": items, "unread": unread}

    @app.post("/api/alerts/events/{event_id}/ack", status_code=204)
    def acknowledge_alert_event(event_id: str) -> Response:
        repo = app.state.repositories["alerts"]
        try:
            repo.acknowledge_event(event_id)
        except KeyError as exc:
            raise _error(404, "事件不存在") from exc
        return Response(status_code=204)

    @app.post("/api/alerts/events/ack-all", status_code=204)
    def acknowledge_all_alert_events() -> Response:
        repo = app.state.repositories["alerts"]
        repo.acknowledge_all()
        return Response(status_code=204)

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
        notifier = getattr(app.state, "notifier", None)
        notifier_cfg = notifier.config() if notifier else None
        fields["notifier_pushplus_enabled"] = {"value": "true" if (notifier_cfg and notifier_cfg.pushplus_enabled) else "false", "source": "sqlite"}
        token_value = (notifier_cfg.pushplus_token if notifier_cfg else "") or ""
        fields["notifier_pushplus_token_set"] = {"value": "true" if token_value else "false", "source": "sqlite"}
        fields["notifier_feishu_enabled"] = {"value": "true" if (notifier_cfg and notifier_cfg.feishu_enabled) else "false", "source": "sqlite"}
        webhook_value = (notifier_cfg.feishu_webhook if notifier_cfg else "") or ""
        fields["notifier_feishu_webhook_set"] = {"value": "true" if webhook_value else "false", "source": "sqlite"}
        monitor_obj = getattr(app.state, "alert_monitor", None)
        monitor_status = monitor_obj.status() if monitor_obj else {}
        fields["notifier_monitor_enabled"] = {"value": "true" if monitor_status.get("enabled") else "false", "source": "sqlite"}
        fields["notifier_monitor_interval_seconds"] = {"value": str(monitor_status.get("interval_seconds") or 60), "source": "sqlite"}
        fields["notifier_monitor_running"] = {"value": "true" if monitor_status.get("running") else "false", "source": "sqlite"}
        return {"schema_version": 1, "fields": fields, "strategies": [{"id": k, "providers": v["providers"], "available": next((s["available"] for s in catalog["strategies"] if s["id"] == k), False)} for k, v in QUOTE_STRATEGIES.items()], "provider_health": {item["provider"]: item for item in provider_health_repo.list()}}

    @app.patch("/api/settings/quote-strategy")
    def update_quote_strategy(payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a user-chosen quote strategy. Validates against QUOTE_STRATEGIES."""
        strategy_id = (payload or {}).get("strategy_id")
        if not isinstance(strategy_id, str) or strategy_id not in QUOTE_STRATEGIES:
            raise _error(status.HTTP_400_BAD_REQUEST, "unknown quote strategy")
        settings_repo.set("quote_strategy_id", strategy_id, source="sqlite")
        catalog = market_data_catalog(active_config, settings_repo.all())
        return {
            "strategy": {
                "id": strategy_id,
                "providers": QUOTE_STRATEGIES[strategy_id]["providers"],
            },
            "fields": {key: catalog[key] for key in ("quote_strategy_id", "quote_provider_chain", "quote_ttl_seconds")},
        }

    @app.patch("/api/settings/notifier")
    def update_notifier(payload: dict[str, Any]) -> dict[str, Any]:
        """Save notifier channel config (pushplus token / feishu webhook / monitor)."""
        data = payload or {}
        # Support both per-channel and unified payload
        # Per-channel: {"channel": "pushplus"|"feishu", "enabled": bool, "token"|"webhook": str}
        channel = data.get("channel")
        if channel == "pushplus":
            if "enabled" in data:
                enabled = str(data.get("enabled")).strip().lower() in ("1", "true", "yes", "on")
                settings_repo.set("notifier.pushplus_enabled", "true" if enabled else "false")
            if "token" in data:
                token = str(data.get("token") or "").strip()
                if token and len(token) > 256:
                    raise _error(status.HTTP_400_BAD_REQUEST, "token 过长")
                settings_repo.set("notifier.pushplus_token", token)
        elif channel == "feishu":
            if "enabled" in data:
                enabled = str(data.get("enabled")).strip().lower() in ("1", "true", "yes", "on")
                settings_repo.set("notifier.feishu_enabled", "true" if enabled else "false")
            if "webhook" in data:
                webhook = str(data.get("webhook") or "").strip()
                if webhook and not webhook.startswith(("http://", "https://")):
                    raise _error(status.HTTP_400_BAD_REQUEST, "webhook 必须是 http(s) URL")
                if webhook and len(webhook) > 512:
                    raise _error(status.HTTP_400_BAD_REQUEST, "webhook 过长")
                settings_repo.set("notifier.feishu_webhook", webhook)
        elif channel == "monitor":
            if "enabled" in data:
                enabled = str(data.get("enabled")).strip().lower() in ("1", "true", "yes", "on")
                settings_repo.set("notifier.monitor_enabled", "true" if enabled else "false")
            if "interval_seconds" in data:
                try:
                    secs = int(data.get("interval_seconds"))
                    secs = max(15, min(600, secs))
                except (TypeError, ValueError):
                    raise _error(status.HTTP_400_BAD_REQUEST, "interval_seconds 必须是 15-600 的整数")
                settings_repo.set("notifier.monitor_interval_seconds", str(secs))
        else:
            # Legacy payload (pushplus only, kept for backward compatibility)
            enabled_raw = data.get("enabled")
            token_raw = data.get("token")
            if enabled_raw is not None:
                enabled = str(enabled_raw).strip().lower() in ("1", "true", "yes", "on")
                settings_repo.set("notifier.pushplus_enabled", "true" if enabled else "false")
            if token_raw is not None:
                token = str(token_raw).strip()
                if token and len(token) > 256:
                    raise _error(status.HTTP_400_BAD_REQUEST, "token 过长")
                settings_repo.set("notifier.pushplus_token", token)
        notifier = getattr(app.state, "notifier", None)
        cfg = notifier.config() if notifier else None
        monitor = getattr(app.state, "alert_monitor", None)
        if monitor is not None:
            monitor.refresh_config()
        monitor_status = monitor.status() if monitor is not None else {}
        return {
            "pushplus_enabled": bool(cfg.pushplus_enabled) if cfg else False,
            "pushplus_token_set": bool(cfg.pushplus_token) if cfg else False,
            "feishu_enabled": bool(cfg.feishu_enabled) if cfg else False,
            "feishu_webhook_set": bool(cfg.feishu_webhook) if cfg else False,
            "monitor_enabled": bool(monitor_status.get("enabled")) if monitor_status else False,
            "monitor_interval_seconds": int(monitor_status.get("interval_seconds") or 60),
            "monitor_running": bool(monitor_status.get("running")),
        }

    @app.post("/api/notifier/monitor/run")
    def trigger_monitor_sweep() -> dict[str, Any]:
        """Trigger one alert-monitor sweep right now (useful for manual testing)."""
        monitor = getattr(app.state, "alert_monitor", None)
        if monitor is None:
            raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "monitor not ready")
        summary = monitor.run_once()
        return summary

    @app.get("/api/notifier/monitor/status")
    def monitor_status() -> dict[str, Any]:
        monitor = getattr(app.state, "alert_monitor", None)
        if monitor is None:
            raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "monitor not ready")
        return monitor.status()

    @app.post("/api/notifier/test")
    def send_notifier_test(channel: str | None = Query(None)) -> dict[str, Any]:
        """Send a test notification. Optional ?channel=pushplus|feishu to test one."""
        notifier = getattr(app.state, "notifier", None)
        if notifier is None:
            raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "notifier not ready")
        result = notifier.send_test(channel)
        if result.get("ok"):
            return result
        # Build informative error message
        errs = []
        for ch, r in (result.get("channels") or {}).items():
            errs.append(f"{ch}: {r.get('error') or r.get('msg') or 'unknown'}")
        raise _error(status.HTTP_400_BAD_REQUEST, "; ".join(errs) or result.get("error") or "推送失败")

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

    # ═══════════════════════════════════════════════════
    # Stage C — Agent Chat (Day 2: 4 endpoints)
    # ═══════════════════════════════════════════════════

    def _stage_c_data_dir() -> Path:
        """Stage C agent 数据目录 ~/.tradingagents/"""
        return Path.home() / ".tradingagents"

    def _stage_c_llm():
        """构造 Stage C chat 用的 LLM。

        - STAGE_C_MOCK_LLM=1 → mock LLM(免 API key)
        - 否则从 active_config 读 quick_model/provider 构造 OpenAIClient
        - 失败时 fallback 到 mock
        """
        import os

        class _MockLLM(Runnable):
            def invoke(self, input, config=None, **kwargs):
                return AIMessage(content="[mock] Stage C fake answer")

            def bind_tools(self, tools):
                return self

        if os.environ.get("STAGE_C_MOCK_LLM") == "1":
            LOGGER.info("Stage C: using mock LLM (STAGE_C_MOCK_LLM=1)")
            return _MockLLM()

        try:
            from tradingagents.llm_clients.openai_client import OpenAIClient
            mc = resolve_model_config(active_config, None, None, None)
            llm = OpenAIClient(
                model=mc["quick_model"], provider=mc["provider"],
            ).get_llm()
            LOGGER.info("Stage C: using %s/%s", mc["provider"], mc["quick_model"])
            return llm
        except Exception as e:
            LOGGER.warning("Stage C: real LLM init failed (%s), fallback to mock", e)
            return _MockLLM()

    @app.post(
        "/api/agent/sessions", status_code=status.HTTP_201_CREATED,
    )
    def create_agent_session() -> dict[str, Any]:
        """创建新的 chat session,返回 session_id。"""
        import uuid
        sid = f"s_{uuid.uuid4().hex[:16]}"
        return {
            "session_id": sid,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }

    @app.get("/api/agent/sessions")
    def list_agent_sessions() -> list[dict[str, Any]]:
        """列出所有 L1 短期对话 sessions。"""
        from tradingagents.agents.general.memory import list_session_ids
        sids = list_session_ids(_stage_c_data_dir())
        return [{"session_id": sid, "last_active": None} for sid in sids]

    @app.get("/api/agent/sessions/{session_id}")
    def get_agent_session(session_id: str) -> dict[str, Any]:
        """读 session 的对话历史(L1 LangGraph state)。"""
        from tradingagents.agents.general.orchestrator import (
            build_agent, get_session_history,
        )
        llm = _stage_c_llm()
        agent, conn = build_agent(
            llm=llm, data_dir=_stage_c_data_dir(), session_id=session_id,
        )
        try:
            history = get_session_history(agent, session_id)
            return {"session_id": session_id, "history": history}
        finally:
            conn.close()

    @app.post("/api/agent/chat/stream")
    def agent_chat_stream(
        request: Request, body: dict[str, Any],
    ) -> StreamingResponse:
        """SSE 流式 chat endpoint — Stage C 主入口。"""
        from tradingagents.agents.general.orchestrator import (
            build_agent, stream_chat,
        )
        session_id = body.get("session_id")
        user_message = body.get("user_message")
        if not session_id or not user_message:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "session_id 和 user_message 必填",
            )

        llm = _stage_c_llm()
        agent, conn = build_agent(
            llm=llm, data_dir=_stage_c_data_dir(), session_id=session_id,
        )

        def stream():
            try:
                for event_type, payload in stream_chat(
                    agent, session_id, user_message,
                ):
                    if _request_disconnected(request):
                        return
                    envelope = {"event": event_type, "payload": payload}
                    yield (
                        f"event: {event_type}\n"
                        f"data: {json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    )
                yield "event: done\ndata: {}\n\n"
            finally:
                conn.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ═══════════════════════════════════════════════════
    # Stage C Day 3: HITL confirm + audit log endpoints
    # ═══════════════════════════════════════════════════

    @app.post("/api/agent/sessions/{session_id}/confirm")
    def confirm_agent_write(
        session_id: str,
        request: Request,
        body: dict[str, Any],
    ) -> StreamingResponse:
        """HITL: 处理用户对写操作的确认/拒绝决策。

        Body:
          {
            "audit_id": int,
            "tool_name": str,       # 用于 grant_approval
            "tool_args": dict,       # 用于 grant_approval
            "approve": bool,         # True=确认,False=拒绝
            "user_message": str,     # 用于 resume 时 replay
          }

        Returns: SSE stream(继续 agent 调用,执行已批准的 tool)
        """
        from tradingagents.agents.general.approval import grant_approval
        from tradingagents.agents.general.audit import update_write_status
        from tradingagents.agents.general.orchestrator import (
            build_agent, stream_chat,
        )

        audit_id = body.get("audit_id")
        tool_name = body.get("tool_name")
        tool_args = body.get("tool_args") or {}
        approve = bool(body.get("approve", False))
        user_message = body.get("user_message", "")

        if not tool_name or not user_message:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "tool_name + user_message 必填",
            )

        # 1. 更新 audit log 状态
        try:
            status_value = "confirmed" if approve else "rejected"
            update_write_status(
                None, audit_id, status=status_value, confirmed_by="user",
            )
        except Exception as e:
            LOGGER.warning("Stage C: audit update failed: %s", e)

        # 2. grant approval(只有 approve=true 才需要)
        if approve:
            grant_approval(session_id, tool_name, tool_args)

        llm = _stage_c_llm()
        agent, conn = build_agent(
            llm=llm, data_dir=_stage_c_data_dir(), session_id=session_id,
        )

        # 3. 状态注入 + 真实执行(避免无限循环)
        # 思路:不要让 agent 从头跑(那会再调同一个 tool → 又 AWAITING_CONFIRMATION)
        # 而是手动:
        #   - approved → 手动执行 tool → inject ToolMessage(content=真实结果)
        #   - rejected → inject ToolMessage(content="user rejected ...")
        # 然后 agent.stream(None, config) 从当前 state resume
        from langchain_core.messages import ToolMessage
        from tradingagents.agents.general.orchestrator import session_thread_id
        from tradingagents.agents.general.tools_bridge import ALL_TOOLS

        config = {"configurable": {"thread_id": session_thread_id(session_id)}}
        tool_call_id = body.get("tool_call_id") or f"call-{audit_id}"
        inject_msg = None
        try:
            if approve:
                tool_obj = next(
                    (t for t in ALL_TOOLS if t.name == tool_name), None,
                )
                if tool_obj is None:
                    result_str = f"ERROR: tool {tool_name!r} not found"
                else:
                    try:
                        invoke_args = {**tool_args, "session_id": session_id}
                        result_str = tool_obj.invoke(invoke_args)
                    except Exception as e:
                        result_str = (
                            f"ERROR: tool execution failed - "
                            f"{type(e).__name__}: {e}"
                        )
                inject_msg = ToolMessage(
                    content=result_str, tool_call_id=tool_call_id,
                )
            else:
                inject_msg = ToolMessage(
                    content=(
                        f"用户拒绝了你的 {tool_name} 操作 "
                        f"(参数:{json.dumps(tool_args, ensure_ascii=False)})。"
                        f"此操作未被执行。请告知用户决定,不要再调用同一个 tool。"
                    ),
                    tool_call_id=tool_call_id,
                )
            if inject_msg is not None:
                agent.update_state(config, {"messages": [inject_msg]})
        except Exception as e:
            LOGGER.warning("Stage C: failed to inject state: %s", e)

        def stream():
            try:
                # 先 emit audit_decision(让前端知道决策被记录了)
                yield (
                    f"event: audit_decision\n"
                    f"data: {json.dumps({'audit_id': audit_id, 'approved': approve, 'tool_name': tool_name}, ensure_ascii=False)}\n\n"
                )
                # resume: 用 update_state 后的 state 继续(传 {} 让 graph 重新跑 LLM)
                from langchain_core.messages import (
                    AIMessage, SystemMessage,
                )
                for chunk in agent.stream(
                    {}, config=config, stream_mode="updates",
                ):
                    if _request_disconnected(request):
                        return
                    if not isinstance(chunk, dict):
                        continue
                    # updates mode: chunk = {node_name: state_updates}
                    for _node, state_updates in chunk.items():
                        if not isinstance(state_updates, dict):
                            continue
                        for msg_chunk in state_updates.get("messages", []) or []:
                            if isinstance(msg_chunk, AIMessage):
                                content = (
                                    msg_chunk.content
                                    if isinstance(msg_chunk.content, str)
                                    else str(msg_chunk.content)
                                )
                                if content:
                                    envelope = {
                                        "event": "reasoning",
                                        "payload": {"content": content},
                                    }
                                    yield (
                                        f"event: reasoning\n"
                                        f"data: {json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))}\n\n"
                                    )
                            elif isinstance(msg_chunk, ToolMessage):
                                content = (
                                    msg_chunk.content
                                    if isinstance(msg_chunk.content, str)
                                    else str(msg_chunk.content)
                                )
                                # 再次检测 AWAITING_CONFIRMATION(LLM 可能还想再调写工具)
                                if isinstance(content, str) and content.startswith("AWAITING_CONFIRMATION:"):
                                    try:
                                        payload = json.loads(content.split(":", 1)[1].strip())
                                    except (json.JSONDecodeError, IndexError):
                                        payload = {"raw": content}
                                    envelope = {
                                        "event": "confirm_request",
                                        "payload": {
                                            "tool_call_id": msg_chunk.tool_call_id,
                                            "tool_name": payload.get("tool_name"),
                                            "tool_args": payload.get("tool_args", {}),
                                            "impact": payload.get("impact"),
                                            "audit_id": payload.get("audit_id"),
                                        },
                                    }
                                    yield (
                                        f"event: confirm_request\n"
                                        f"data: {json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))}\n\n"
                                    )
                                    return  # 暂停,等下一次 confirm
                                envelope = {
                                    "event": "tool_result",
                                    "payload": {
                                        "tool_call_id": msg_chunk.tool_call_id,
                                        "name": getattr(
                                            msg_chunk, "name", None,
                                        ) or "(tool)",
                                        "content": content,
                                    },
                                }
                                yield (
                                    f"event: tool_result\n"
                                    f"data: {json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))}\n\n"
                                )
                            elif isinstance(msg_chunk, SystemMessage):
                                pass
                        yield "event: done\ndata: {}\n\n"
            finally:
                conn.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/agent/audit")
    def list_audit_log(
        session_id: str | None = Query(None),
        status: str | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        """读 Stage C 写操作 audit log(管理 UI 用)。"""
        from tradingagents.agents.general.audit import list_writes
        items = list_writes(
            session_id=session_id, status=status, limit=limit,
        )
        return {"items": items, "total": len(items), "limit": limit}

    @app.post("/api/agent/audit/{audit_id}/status")
    def update_audit_status(
        audit_id: int, body: dict[str, Any],
    ) -> dict[str, Any]:
        """手动更新 audit log 状态(管理 UI 用)。"""
        from tradingagents.agents.general.audit import update_write_status
        new_status = body.get("status")
        if not new_status:
            raise _error(422, "status 必填")
        ok = update_write_status(
            None, audit_id, status=new_status,
            confirmed_by=body.get("confirmed_by"),
            error=body.get("error"),
        )
        return {"ok": ok, "audit_id": audit_id, "status": new_status}

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
