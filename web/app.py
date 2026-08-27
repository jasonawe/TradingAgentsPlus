"""FastAPI transport for the local TradingAgents analysis console."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from tradingagents.default_config import DEFAULT_CONFIG

from .history import ReportHistory, ReportNotFound
from .manager import ActiveRunError, EventBatch, RunManager
from .models import AnalysisRequest, EventEnvelope, RunRecord
from .runner import WebRunRunner
from .config import OUTPUT_LANGUAGES, model_catalog, resolve_model_config
from .repositories import (
    AnalysisRunRepository,
    QuoteRepository,
    ReportRepository,
    SettingsRepository,
    SnapshotRepository,
    WatchlistRepository,
)
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
        "providers": providers,
        "configured": configured,
        # Keep these aliases for older clients.
        "provider": configured["provider"],
        "model": configured["deep_model"],
    }


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
    active_manager = manager or RunManager(store=store)
    if manager is not None and getattr(manager, "_store", None) is None:
        manager._store = store
        manager._db_path = store.path
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
        "settings": SettingsRepository(store),
        "reports": ReportRepository(store),
    }

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "invalid analysis request"}, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> Response:
        index_path = _STATIC_DIR / "index.html"
        if index_path.is_file():
            return FileResponse(index_path, media_type="text/html")
        return HTMLResponse("<!doctype html><title>TradingAgents</title><h1>TradingAgents</h1>")

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return _config_view(active_config)

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
        except ValueError as exc:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid analysis configuration") from exc
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
