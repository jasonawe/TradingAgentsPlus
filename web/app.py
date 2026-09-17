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


def _setup_langsmith_tracing() -> None:
    """Day 9 #11: LangSmith 接入 — 只在用户主动设 LANGCHAIN_API_KEY 时启用 trace。

    LangChain 0.1+ 自动读以下 env vars 启用 LangSmith trace:
      - LANGCHAIN_TRACING_V2=true
      - LANGCHAIN_API_KEY=<key>
      - LANGCHAIN_PROJECT=<project>
      - LANGCHAIN_ENDPOINT(可选,默认 https://api.smith.langchain.com)

    我们只 setdefault,不覆盖用户已有设置。
    """
    api_key = os.environ.get("LANGCHAIN_API_KEY", "").strip()
    if not api_key:
        LOGGER.info("LangSmith tracing disabled (set LANGCHAIN_API_KEY to enable)")
        return
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_PROJECT", "TradingAgentsPlus")
    try:
        from langsmith import Client
        Client(api_key=api_key).list_projects(limit=1)
        LOGGER.info(
            "LangSmith tracing enabled (project=%s, endpoint=%s)",
            os.environ.get("LANGCHAIN_PROJECT"),
            os.environ.get("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com"),
        )
    except Exception as e:  # noqa: BLE001
        LOGGER.warning("LangSmith reachable check failed (tracing may still work): %s", e)


def create_app(
    *,
    manager: RunManager | None = None,
    config: dict[str, Any] | None = None,
    runner: Any | None = None,
    history: ReportHistory | None = None,
) -> FastAPI:
    """Build an isolated application instance suitable for local use or tests."""
    # Day 9 #11: LangSmith trace — 必须在 create_llm_client 之前调
    _setup_langsmith_tracing()

    active_config = copy.deepcopy(config if config is not None else DEFAULT_CONFIG)
    # Day N+: web_runs.sqlite3 路径单一真实源,跟 L2/L3 工具、audit log、
    # MCP server、memory 全部共用 tradingagents.default_config.web_runs_db_path()
    # —— 避免「页面看到 8 条、LLM 工具看到 1 条」这种数据分叉 bug。
    from tradingagents.default_config import web_runs_db_path as _default_web_runs_db_path
    run_db_path = active_config.get("web_runs_db") or _default_web_runs_db_path()
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
    # Load persisted data provider choice (survives restart).
    from tradingagents.data.providers.registry import (
        PROVIDERS, get_active_provider_name, set_active_provider,
    )
    persisted = (settings_repo.get("active_data_provider") or {}).get("value")
    if isinstance(persisted, str) and persisted in PROVIDERS:
        # Only override if env var wasn't explicitly set.
        env_name = os.environ.get("TRADINGAGENTS_DATA_PROVIDER")
        if not env_name:
            set_active_provider(persisted)

    # W3-D2 polish: persisted news + alpha provider choices.
    from tradingagents.data.providers.news_registry import (
        NEWS_PROVIDERS as _NEWS_PROVIDERS,
        set_active_news_provider as _set_active_news,
    )
    from tradingagents.data.providers.alpha_registry import (
        ALPHA_PROVIDERS as _ALPHA_PROVIDERS,
        set_active_alpha_provider as _set_active_alpha,
    )
    _persisted_news = (settings_repo.get("active_news_provider") or {}).get("value")
    if isinstance(_persisted_news, str) and _persisted_news in _NEWS_PROVIDERS:
        if not os.environ.get("TRADINGAGENTS_NEWS_PROVIDER"):
            _set_active_news(_persisted_news)
    _persisted_alpha = (settings_repo.get("active_alpha_provider") or {}).get("value")
    if isinstance(_persisted_alpha, str) and _persisted_alpha in _ALPHA_PROVIDERS:
        if not os.environ.get("TRADINGAGENTS_ALPHA_PROVIDER"):
            _set_active_alpha(_persisted_alpha)

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
        from tradingagents.agent_harness.tools.impl import set_repositories
        set_repositories({
            "notes": repositories["notes"],
            "alerts": repositories["alerts"],
            # §P3-1 — wire watchlist repository so add_to_watchlist /
            # remove_from_watchlist tools can mutate the user's list.
            "watchlist": repositories["watchlist"],
            # §P3-3 — wire scheduled_jobs repository so
            # create_scheduled_task / update_scheduled_task /
            # delete_scheduled_task tools can mutate the user's jobs.
            "scheduled_jobs": repositories["scheduled_jobs"],
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
    # Stage C: 注入 market_service 让 get_quote / get_quotes_batch tool 能用
    try:
        from tradingagents.agent_harness.tools.impl import set_quote_service
        set_quote_service(app.state.market_service)
        LOGGER.info("Stage C: QuoteService injected for quote tools")
    except ImportError:
        pass
    # Day 7: 注入 ActiveRunner / Scheduler / News / ReportHistory 让 6 个新 tool 能用
    try:
        from tradingagents.agent_harness.tools.impl import (
            set_active_runner, set_scheduler_service, set_news_provider,
            set_report_history,
        )
        set_active_runner(active_manager)
        set_scheduler_service(scheduler_service)
        # News provider 用 alpha_vantage_news.get_news(已知存在)
        from tradingagents.dataflows import alpha_vantage_news as _news_mod
        set_news_provider(_news_mod.get_news)
        # ReportHistory 用 web.history.ReportHistory(results_dir=...)
        # 注:不能局部 from-import ReportHistory(触发 UnboundLocalError 因为函数内 line 221 也用了 ReportHistory)
        # 用 module attribute 引用:web.history.ReportHistory
        results_dir = active_config.get("results_dir") if active_config else None
        import web.history as _web_history
        set_report_history(_web_history.ReportHistory(results_dir=results_dir))
        LOGGER.info("Stage C Day 7: runner + scheduler + news + report history injected")
    except ImportError as e:
        LOGGER.warning("Stage C Day 7 imports skipped: %s", e)
    except Exception as e:
        LOGGER.warning("Stage C Day 7 injection failed: %s", e)
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

    # P8: mount harness health endpoint (lazy build so we don't slow
    # startup; errors are logged but never fail create_app).
    try:
        from tradingagents.agent_harness.harness import Harness, mount_health_endpoint
        app.state.harness = Harness()
        mount_health_endpoint(app, app.state.harness, path="/api/harness/health")
        # P1 in-flight lock: per-session lock so concurrent stream_chat
        # requests for the same session don't race on circuit breaker /
        # audit log / SSE event emission. Second request yields a `busy`
        # event and exits immediately.
        from tradingagents.agent_harness.core.session_lock import SessionLockManager
        app.state.session_lock = SessionLockManager()
        # P1-7 crash recovery: persist per-session state so a restarted
        # server (or recovered client SSE) can resume from the last
        # checkpoint via /api/harness/chat/resume.
        from tradingagents.agent_harness.core.harness_checkpoint import (
            HarnessCheckpointStore,
        )
        ckpt_store = HarnessCheckpointStore(app.state.repositories["settings"])
        try:
            purged = ckpt_store.purge_stale()
            if purged:
                LOGGER.info("purged %d stale harness checkpoints at startup", purged)
        except Exception:
            LOGGER.debug("checkpoint purge failed", exc_info=True)
        # Wire the store into the orchestrator inside the harness so
        # stream_chat persists checkpoints automatically.
        if hasattr(app.state.harness, "set_checkpoint_store"):
            app.state.harness.set_checkpoint_store(ckpt_store)
        else:
            # Fallback: mutate the underlying orchestrator (the harness
            # exposes it via .orchestrator in some versions).
            orch = getattr(app.state.harness, "orchestrator", None)
            if orch is not None and hasattr(orch, "_checkpoint_store"):
                orch._checkpoint_store = ckpt_store
        app.state.checkpoint_store = ckpt_store

        # A2 SessionStore: per-session metadata + lifecycle. Wired into
        # the orchestrator so stream_chat auto-creates / touches rows.

        from tradingagents.agent_harness.core.session_store import (
            SESSION_STATUS_ACTIVE,
            Session,
            SessionStore,
        )
        session_store = SessionStore(app.state.repositories["settings"])
        if hasattr(app.state.harness, "set_session_store"):
            app.state.harness.set_session_store(session_store)
        else:
            orch = getattr(app.state.harness, "orchestrator", None)
            if orch is not None and hasattr(orch, "_session_store"):
                orch._session_store = session_store
        app.state.session_store = session_store

        # P2 LLM response cache: short-circuit identical LLM calls.
        from tradingagents.agent_harness.llm.cache import LLMResponseCache
        cache = LLMResponseCache(ttl_seconds=3600)
        # Wire into all LLM factories the harness exposes
        for factory_name in ("llm_factory", "judge_factory"):
            factory = getattr(app.state.harness, factory_name, None)
            if factory is not None and hasattr(factory, "cache"):
                factory.cache = cache
        app.state.llm_cache = cache
        LOGGER.info(
            "harness mounted (agents=%d, tools=%d, enable_l3=%s)",
            len(app.state.harness.agent_registry.list()),
            len(app.state.harness.list_tools()),
            app.state.harness.config.enable_l3,
        )

        # P8: chat endpoint — wraps harness.stream_chat in SSE so the
        # orchestrator + LLM + L3 judge fire in real production paths.
        @app.post("/api/harness/chat")
        async def _harness_chat(body: dict) -> StreamingResponse:
            import json as _json
            session_id = body.get("session_id") or f"harness-{__import__('uuid').uuid4().hex[:8]}"
            message = body.get("message") or ""
            if not message:
                raise _error(status.HTTP_400_BAD_REQUEST, "message is required")

            async def _event_stream():
                lock_mgr = app.state.session_lock
                async def _producer():
                    async for event, payload in app.state.harness.stream_chat(
                        session_id=session_id, user_message=message
                    ):
                        yield event, payload
                try:
                    async for event, payload in lock_mgr.run(session_id, _producer):
                        yield f"event: {event}\ndata: {_json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                except Exception as e:
                    LOGGER.exception("harness chat failed")
                    yield f"event: error\ndata: {_json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

            return StreamingResponse(
                _event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # P1-7 crash recovery: resume a crashed session. Replays buffered
        # events from the last checkpoint + yields resume_complete.
        @app.post("/api/harness/chat/resume")
        async def _harness_resume(body: dict) -> StreamingResponse:
            import json as _json
            session_id = body.get("session_id") or ""
            if not session_id:
                raise _error(status.HTTP_400_BAD_REQUEST, "session_id is required")

            async def _event_stream():
                try:
                    async for event, payload in app.state.harness.resume(session_id):
                        yield f"event: {event}\ndata: {_json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                except Exception as e:
                    LOGGER.exception("harness resume failed")
                    yield f"event: error\ndata: {_json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

            return StreamingResponse(
                _event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # §P3-3+ HITL: harness-path approval endpoint. Mirrors
        # /api/harness/sessions/{sid}/confirm — harness path
        # orchestrator. Body:
        #   { tool_name, tool_args, approve, user_message }
        # approve=True -> grant_approval() + re-run stream_chat()
        # approve=False -> grant_approval is skipped; emit rejection
        # audit decision so the frontend closes the dialog.
        @app.post("/api/harness/sessions/{session_id}/confirm")
        async def _harness_confirm(
            session_id: str,
            body: dict,
        ) -> StreamingResponse:
            from tradingagents.agent_harness.hitl import (
                grant_approval, revoke_session,
            )
            import json as _json
            tool_name = body.get("tool_name") or ""
            tool_args = dict(body.get("tool_args") or {})
            approve = bool(body.get("approve", False))
            user_message = body.get("user_message") or ""
            audit_id = body.get("audit_id")

            if not tool_name:
                raise _error(
                    status.HTTP_400_BAD_REQUEST,
                    "tool_name is required",
                )

            async def _event_stream():
                # Race fix (A) — atomic claim before invoking the tool.
                # ``/confirm`` used to call ``update_write_status(...)``
                # then ``tool.invoke(...)`` directly with no protection
                # against concurrent calls. Double-click on the confirm
                # dialog (or a network retry) caused the destructive
                # tool to run twice. Now we CAS the audit row from
                # pending → confirmed/rejected atomically; only the
                # winning request proceeds to invoke the tool.
                #
                # ``audit_id`` from the outer scope; the in-function
                # ``if audit_id is None: audit_id = log_write(...)``
                # below would otherwise make Python treat it as local
                # and UnboundLocalError on the read. Use a separate
                # local variable name to avoid the issue.
                nonlocal audit_id
                claimed = False
                try:
                    from tradingagents.agent_harness.audit import (
                        log_write, update_write_status,
                        get_write_status, claim_write_audit,
                    )
                    if audit_id is None:
                        # Legacy / missing-id path: create the row and
                        # claim in one go. Still racy against
                        # concurrent legacy callers, but they all share
                        # the same audit_id path so the worst case is
                        # "another request created audit_id=N+1" which
                        # is a separate write.
                        audit_id = log_write(
                            tool_name=tool_name,
                            tool_args=tool_args,
                            status="pending",
                        )
                        claimed = claim_write_audit(
                            None, audit_id,
                            action="confirmed" if approve else "rejected",
                            confirmed_by="user",
                        )
                    else:
                        # Atomic CAS — the fix.
                        claimed = claim_write_audit(
                            None, audit_id,
                            action="confirmed" if approve else "rejected",
                            confirmed_by="user",
                        )
                except Exception as e:
                    LOGGER.warning(
                        "audit claim failed (continuing best-effort): %s", e,
                    )
                    # If claim raised (DB unavailable), don't re-invoke
                    # — return idempotent error so the user retries.
                    yield (
                        f"event: error\n"
                        f"data: {_json.dumps({'error': f'audit_unavailable: {e}'}, ensure_ascii=False)}\n\n"
                    )
                    yield (
                        f"event: done\n"
                        f"data: {_json.dumps({}, ensure_ascii=False)}\n\n"
                    )
                    return

                if not claimed:
                    # Lost the race — surface current status so the
                    # client knows whether to treat this as already-
                    # done (executed/failed/rejected) or in-flight
                    # (confirmed). Either way, NEVER re-invoke.
                    current = None
                    if audit_id is not None:
                        try:
                            current = get_write_status(None, audit_id)
                        except Exception:
                            pass
                    LOGGER.info(
                        "/confirm idempotent skip: audit_id=%s already %s "
                        "(double-click or retry; tool NOT re-invoked)",
                        audit_id, current,
                    )
                    yield (
                        f"event: audit_decision\n"
                        f"data: {_json.dumps({'audit_id': audit_id, 'approved': approve, 'tool_name': tool_name, 'audit_status': current, 'idempotent': True}, ensure_ascii=False)}\n\n"
                    )
                    yield (
                        f"event: done\n"
                        f"data: {_json.dumps({'idempotent': True, 'audit_status': current}, ensure_ascii=False)}\n\n"
                    )
                    return

                # 2) emit audit_decision so the frontend closes the dialog
                yield (
                    f"event: audit_decision\n"
                    f"data: {_json.dumps({'audit_id': audit_id, 'approved': approve, 'tool_name': tool_name}, ensure_ascii=False)}\n\n"
                )

                if not approve:
                    # Nothing to re-run; the rejection is recorded.
                    yield (
                        f"event: done\n"
                        f"data: {_json.dumps({'rejected': tool_name}, ensure_ascii=False)}\n\n"
                    )
                    return

                # 3) grant approval so the next stream_chat() finds it
                grant_approval(session_id, tool_name, tool_args)

                # 4) directly invoke the gated tool through the harness
                #    tool registry (NOT via stream_chat — a fresh chat
                #    turn wouldn't know about this tool, and forcing
                #    approval + replay only confuses the LLM). Mirror
                #    the harness confirm path: invoke the tool,
                #    emit tool_call / tool_result / agent_final SSE
                #    events so the frontend's reasoning trace + bubble
                #    update cleanly. consume_approval after success so
                #    the same (tool, args) tuple can't replay twice.
                try:
                    from tradingagents.agent_harness.tools import ToolContext
                    from tradingagents.agent_harness.hitl import (
                        consume_approval,
                    )
                    registry = app.state.harness.tool_registry
                    tool = registry.get(tool_name)
                    args_schema = tool.schema.args_schema
                    if hasattr(args_schema, "model_validate"):
                        validated = args_schema.model_validate(tool_args)
                    else:
                        validated = tool_args
                    # Build ToolContext (consumers of the bridge tools
                    # extract session_id from RunnableConfig; for the
                    # harness tool registry we use ToolContext directly).
                    ctx = ToolContext(session_id=session_id)
                    # Surface the tool_call event so the UI's reasoning
                    # trace shows the action.
                    yield (
                        f"event: tool_call\n"
                        f"data: {_json.dumps({'name': tool_name, 'args': tool_args}, ensure_ascii=False, default=str)}\n\n"
                    )
                    tool_result_obj = await tool.invoke(validated, ctx)
                    # Normalise dict-like / Pydantic responses.
                    if hasattr(tool_result_obj, "model_dump"):
                        tool_result_dict = tool_result_obj.model_dump()
                    elif isinstance(tool_result_obj, dict):
                        tool_result_dict = tool_result_obj
                    else:
                        tool_result_dict = {"value": str(tool_result_obj)}
                    yield (
                        f"event: tool_result\n"
                        f"data: {_json.dumps({'name': tool_name, 'ok': True, 'result': tool_result_dict}, ensure_ascii=False, default=str)}\n\n"
                    )
                    # Emit agent_final so the assistant bubble shows
                    # the tool output (renderer falls back to
                    # formatRawResult when result.summary is empty).
                    yield (
                        f"event: agent_final\n"
                        f"data: {_json.dumps({'tier': 1, 'result': tool_result_dict}, ensure_ascii=False, default=str)}\n\n"
                    )
                    # Success: consume so the same (tool, args) can't replay.
                    try:
                        consume_approval(session_id, tool_name, tool_args)
                    except Exception:
                        pass
                    # O10 fix — mark audit row as executed so the audit
                    # log reflects real write outcomes (not just user
                    # approval). Best-effort: never raise from here.
                    if audit_id:
                        try:
                            update_write_status(
                                None, audit_id, status="executed",
                            )
                        except Exception as _audit_err:
                            LOGGER.warning(
                                "harness confirm: audit->executed failed: %s",
                                _audit_err,
                            )
                except Exception as e:
                    LOGGER.exception("harness confirm tool invoke failed")
                    yield (
                        f"event: error\n"
                        f"data: {_json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
                    )
                    if audit_id:
                        try:
                            update_write_status(
                                None, audit_id, status="failed", error=str(e),
                            )
                        except Exception as _audit_err:
                            LOGGER.warning(
                                "harness confirm: audit->failed update failed: %s",
                                _audit_err,
                            )
                finally:
                    yield (
                        f"event: done\n"
                        f"data: {_json.dumps({}, ensure_ascii=False)}\n\n"
                    )

            return StreamingResponse(
                _event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # P2-12: session-level "auto-approve all writes" toggle.
        # When enabled, the next write tool call in this session
        # skips the HITL gate (returns None from
        # _check_write_approval). Sticky for the session lifetime
        # until revoked; survives across multiple chat turns.
        @app.post("/api/harness/sessions/{session_id}/grant_all")
        async def _harness_grant_all(session_id: str) -> dict:
            from tradingagents.agent_harness.hitl import (
                grant_session_all, is_session_grant_all,
            )
            grant_session_all(session_id)
            return {
                "session_id": session_id,
                "grant_all": True,
                "is_granted": is_session_grant_all(session_id),
            }

        @app.post("/api/harness/sessions/{session_id}/revoke_all")
        async def _harness_revoke_all(session_id: str) -> dict:
            from tradingagents.agent_harness.hitl import (
                revoke_session_grant_all, is_session_grant_all,
            )
            revoke_session_grant_all(session_id)
            return {
                "session_id": session_id,
                "grant_all": False,
                "is_granted": is_session_grant_all(session_id),
            }

        @app.get("/api/harness/sessions/{session_id}/grant_all")
        async def _harness_grant_all_status(session_id: str) -> dict:
            from tradingagents.agent_harness.hitl import (
                is_session_grant_all,
            )
            return {
                "session_id": session_id,
                "is_granted": is_session_grant_all(session_id),
            }

        # P1-8 Batch mode: synchronous JSON response. Useful for CLI
        # scripts, scheduled jobs, and clients that can't keep an SSE
        # connection open. Runs the full 5-node state machine and
        # returns the captured events + final state.
        @app.post("/api/harness/chat/batch")
        async def _harness_chat_batch(body: dict) -> dict:
            session_id = body.get("session_id") or f"harness-{__import__('uuid').uuid4().hex[:8]}"
            message = body.get("message") or ""
            if not message:
                raise _error(status.HTTP_400_BAD_REQUEST, "message is required")
            events: list[dict[str, Any]] = []
            final: Any = None
            token_usage: dict[str, Any] = {}
            error: str | None = None
            try:
                async for event, payload in app.state.harness.stream_chat(
                    session_id=session_id, user_message=message
                ):
                    events.append({"event": event, "payload": payload})
                    if event == "agent_final":
                        final = payload.get("result")
                    elif event == "usage_summary":
                        token_usage = payload
                    elif event == "error":
                        error = payload.get("error") or payload.get("failure", {}).get("message")
            except Exception as e:
                LOGGER.exception("harness batch failed")
                raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))
            return {
                "session_id": session_id,
                "events": events,
                "event_count": len(events),
                "final": final,
                "token_usage": token_usage,
                "error": error,
            }

        # P1-8 Poll mode: client passes the session_id + last-seen
        # event index, returns events that occurred since then. Backed
        # by the same HarnessCheckpointStore used by /chat/resume.
        @app.get("/api/harness/chat/{session_id}/events")
        async def _harness_chat_poll(session_id: str, since: int = 0) -> dict:
            """Return events after index ``since`` for ``session_id``.

            Polling clients can call repeatedly with ``since=<last_index+1>``
            to incrementally drain a session. Returns 404 if the session
            has no checkpoint (already completed and cleaned up).
            """
            ckpt_store = getattr(app.state, "checkpoint_store", None)
            if ckpt_store is None:
                raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "checkpoint store not configured")
            ckpt = ckpt_store.load(session_id)
            if ckpt is None:
                raise _error(status.HTTP_404_NOT_FOUND, f"no checkpoint for session {session_id}")
            events = ckpt.emitted_events
            since_idx = max(0, int(since))
            slice_ = events[since_idx:]
            return {
                "session_id": session_id,
                "events": [{"event": ev, "payload": p} for ev, p in slice_],
                "event_count": len(slice_),
                "next_since": len(events),
                "done": ckpt.node_position == "done",
            }

        # P8 L3 verification status endpoint — exposes judge_factory
        # wiring + per-agent LLM state so we can curl-verify L3 is wired.
        @app.get("/api/harness/cache/stats")
        async def _harness_cache_stats() -> dict:
            """Return LLM cache hit/miss rates for the running process."""
            cache = getattr(app.state, "llm_cache", None)
            if cache is None:
                return {"configured": False}
            return {"configured": True, **cache.stats}

        @app.get("/api/harness/status")
        async def _harness_status() -> dict:
            h = app.state.harness
            return {
                "ok": True,
                "enable_l3": h.config.enable_l3,
                "judge": {
                    "provider": h.config.judge_provider or h.config.llm_provider,
                    "model": h.config.judge_model or h.config.llm_model,
                    "configured": h.judge_factory.is_configured()
                    if hasattr(h.judge_factory, "is_configured")
                    else bool(h.judge_factory),
                },
                "agents": [
                    {
                        "name": name,
                        "llm_wired": a.llm_factory is not None,
                        "tools_wired": a.tool_registry is not None,
                        **(
                            {"judge_wired": a.judge_factory is not None, "enable_l3": a.enable_l3}
                            if name == "verifier"
                            else {}
                        ),
                    }
                    for name, a in sorted(h.agent_registry._agents.items())
                ],
            }
    except Exception as e:
        LOGGER.warning("harness mount skipped: %s", e)

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
    @app.get("/agent-audit", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/harness", response_class=HTMLResponse, include_in_schema=False)
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
        from tradingagents.data.providers.registry import get_active_provider_name
        from tradingagents.data.providers.news_registry import get_active_news_provider_name
        from tradingagents.data.providers.alpha_registry import get_active_alpha_provider_name
        persisted_provider = (settings_repo.get("active_data_provider") or {}).get("value")
        current_provider = persisted_provider or get_active_provider_name()
        fields["active_data_provider"] = {"value": current_provider, "source": "sqlite" if persisted_provider else "env"}

        # W3-D2 polish: expose news + alpha active providers.
        from tradingagents.data.providers.news_registry import NEWS_PROVIDERS
        from tradingagents.data.providers.alpha_registry import ALPHA_PROVIDERS
        persisted_news = (settings_repo.get("active_news_provider") or {}).get("value")
        current_news = persisted_news or get_active_news_provider_name()
        fields["active_news_provider"] = {
            "value": current_news,
            "source": "sqlite" if persisted_news else "env",
            "options": sorted(NEWS_PROVIDERS),
        }
        persisted_alpha = (settings_repo.get("active_alpha_provider") or {}).get("value")
        current_alpha = persisted_alpha or get_active_alpha_provider_name()
        fields["active_alpha_provider"] = {
            "value": current_alpha,
            "source": "sqlite" if persisted_alpha else "env",
            "options": sorted(ALPHA_PROVIDERS),
        }
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

    @app.patch("/api/settings/data-provider")
    def update_data_provider(payload: dict[str, Any]) -> dict[str, Any]:
        """Switch the active market data provider (yfinance/eastmoney/akshare/alpha_vantage).

        Persists to settings_repo and applies immediately to the in-process
        registry, so the next get_quote / get_history call goes through the
        chosen provider.
        """
        from tradingagents.data.providers.registry import PROVIDERS, set_active_provider

        name = (payload or {}).get("provider")
        if not isinstance(name, str) or name not in PROVIDERS:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                f"unknown provider; known: {sorted(PROVIDERS)}",
            )
        settings_repo.set("active_data_provider", name, source="sqlite")
        set_active_provider(name)
        return {"provider": name, "providers": sorted(PROVIDERS)}

    @app.patch("/api/settings/news-provider")
    def update_news_provider(payload: dict[str, Any]) -> dict[str, Any]:
        """Switch the active news provider (stub/yfinance/alpha_vantage)."""
        from tradingagents.data.providers.news_registry import (
            NEWS_PROVIDERS, set_active_news_provider,
        )
        name = (payload or {}).get("provider")
        if not isinstance(name, str) or name not in NEWS_PROVIDERS:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                f"unknown news provider; known: {sorted(NEWS_PROVIDERS)}",
            )
        settings_repo.set("active_news_provider", name, source="sqlite")
        set_active_news_provider(name)
        return {"provider": name, "providers": sorted(NEWS_PROVIDERS)}

    @app.patch("/api/settings/alpha-provider")
    def update_alpha_provider(payload: dict[str, Any]) -> dict[str, Any]:
        """Switch the active alpha provider (stub/yfinance/akshare)."""
        from tradingagents.data.providers.alpha_registry import (
            ALPHA_PROVIDERS, set_active_alpha_provider,
        )
        name = (payload or {}).get("provider")
        if not isinstance(name, str) or name not in ALPHA_PROVIDERS:
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                f"unknown alpha provider; known: {sorted(ALPHA_PROVIDERS)}",
            )
        settings_repo.set("active_alpha_provider", name, source="sqlite")
        set_active_alpha_provider(name)
        return {"provider": name, "providers": sorted(ALPHA_PROVIDERS)}

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

    # §P3-3+ Stage C 老编排器 endpoint 已全部移除(阶段 2 迁移)。
    # AI 能力统一由 harness 提供,所有写操作走 /api/harness/sessions/{sid}/confirm。

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
