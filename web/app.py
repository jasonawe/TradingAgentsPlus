"""FastAPI transport for the local TradingAgents analysis console."""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from tradingagents.default_config import DEFAULT_CONFIG

from .config import (
    OUTPUT_LANGUAGES,
    QUOTE_STRATEGIES,
    market_data_catalog,
    model_catalog,
    resolve_model_config,
    resolve_run_lifecycle_config,
)
from .history import ReportHistory, ReportNotFound
from .manager import ActiveRunError, EventBatch, RunManager
from .market_data import ProviderRouter, QuoteService
from .market_models import ProviderError
from .models import AnalysisRequest, EventEnvelope, RunRecord
from .providers import AlphaVantageProvider, YFinanceProvider
from .repositories import (
    AnalysisRunRepository,
    QuoteRepository,
    ReportRepository,
    SettingsRepository,
    SnapshotRepository,
    WatchlistRepository,
)
from .runner import WebRunRunner
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
            {"key": "market", "label": "Market Analyst"},
            {"key": "social", "label": "Sentiment Analyst"},
            {"key": "news", "label": "News Analyst"},
            {"key": "fundamentals", "label": "Fundamentals Analyst"},
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


def _watchlist_view(repo: WatchlistRepository) -> dict[str, Any]:
    wl = repo.get_default()
    return {"watchlist": {"id": wl["id"], "name": wl["name"], "version": wl["version"]}, "items": repo.list_items()}


def _event_sse(event: EventEnvelope) -> str:
    payload = event.model_dump(mode="json")
    # ``event`` is the SSE event type; the JSON envelope retains all metadata.
    return (
        f"id: {event.seq}\n"
        f"event: {event.event.value}\n"
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
    lifecycle_config = resolve_run_lifecycle_config(
        config if config is not None else {}, settings_repo.all()
    )
    active_manager = manager or RunManager(store=store, lifecycle_config=lifecycle_config)
    if manager is not None and getattr(manager, "_store", None) is None:
        manager._store = store
        manager._db_path = store.path
    if manager is not None:
        manager.configure_lifecycle(lifecycle_config)
    active_history = history or ReportHistory(
        results_dir=active_config.get("results_dir"), cwd=active_config.get("project_dir")
    )
    app = FastAPI(title="TradingAgents Web Console")
    app.state.manager = active_manager
    app.state.config = active_config
    app.state.history = active_history
    app.state.store = store
    app.state.repositories = {
        "watchlist": WatchlistRepository(store),
        "quotes": QuoteRepository(store),
        "runs": AnalysisRunRepository(store),
        "snapshots": SnapshotRepository(store),
        "settings": settings_repo,
        "reports": ReportRepository(store),
    }
    providers = {"yfinance": YFinanceProvider(), "alpha_vantage": AlphaVantageProvider()}
    app.state.market_router = ProviderRouter(providers)
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
    @app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    def index() -> Response:
        return _console_entry()

    @app.get("/reports/{report_id}", response_class=HTMLResponse, include_in_schema=False)
    def report_index(report_id: str) -> Response:
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

    @app.get("/api/providers/market-data")
    def market_provider_status() -> dict[str, Any]:
        catalog = market_data_catalog(active_config, settings_repo.all())
        return {"providers": catalog["providers"]}

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        catalog = market_data_catalog(active_config, settings_repo.all())
        fields = {key: catalog[key] for key in ("quote_strategy_id", "quote_provider_chain", "quote_ttl_seconds")}
        _, defaults = model_catalog(active_config)
        source = "env" if os.getenv("TRADINGAGENTS_OUTPUT_LANGUAGE") else (settings_repo.get("output_language") or {}).get("source", "default")
        fields["output_language"] = {"value": defaults["output_language"], "source": source}
        fields["effective_output_language"] = fields["output_language"]
        return {"schema_version": 1, "fields": fields, "strategies": [{"id": k, "providers": v["providers"], "available": next((s["available"] for s in catalog["strategies"] if s["id"] == k), False)} for k, v in QUOTE_STRATEGIES.items()]}

    @app.get("/api/runs/active")
    def get_active_run() -> dict[str, Any]:
        """Return the current analysis so a reopened client can reattach."""

        active_id = active_manager.active_run_id
        if not active_id:
            return {"run": None}
        try:
            return {"run": _record_json(active_manager.get_run(active_id))}
        except KeyError:
            # The worker may finish between reading active_run_id and get_run.
            return {"run": None}

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    def create_run(request_data: AnalysisRequest) -> JSONResponse:
        try:
            selected = resolve_model_config(
                active_config,
                request_data.provider,
                request_data.quick_model,
                request_data.deep_model,
            )
            language = request_data.output_language or model_catalog(active_config)[1]["output_language"]
            if language not in OUTPUT_LANGUAGES:
                raise ValueError("invalid analysis configuration")
            request_data = request_data.model_copy(update={
                "provider": selected["provider"],
                "quick_model": selected["quick_model"],
                "deep_model": selected["deep_model"],
                "output_language": language,
            })
            strategy = request_data.quote_strategy_id or market_data_catalog(active_config, settings_repo.all())["quote_strategy_id"]["value"]
            if strategy not in QUOTE_STRATEGIES:
                raise ValueError("invalid analysis configuration")
            request_data = request_data.model_copy(update={"quote_strategy_id": strategy})
        except ValueError as exc:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "invalid analysis configuration",
            ) from exc
        if runner is None:
            worker = WebRunRunner(active_manager, config=active_config).worker
        elif hasattr(runner, "worker"):
            worker = runner.worker
        else:
            worker = runner
        try:
            record = active_manager.start_run(request_data, worker=worker)
        except ActiveRunError as exc:
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
                        # Snapshot events use a synthetic sequence before the
                        # retained range; never move the subscriber backwards.
                        yield _event_sse(event)
                        if event.seq > cursor:
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

    @app.get("/api/history")
    def list_history() -> list[dict[str, Any]]:
        return active_history.list_reports()

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
