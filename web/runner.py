"""Graph execution adapter for web run manager events."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import traceback
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.propagation import PropagationCancelled
from tradingagents.graph.trading_graph import TradingAgentsGraph

from .artifacts import ArtifactRepository
from .checkpoint_resume import (
    GRAPH_VERSION,
    build_checkpoint_signature,
    retention_until,
)
from .error_classifier import (
    classify_provider_exception,
)
from .error_codes import USER_MESSAGES, TerminalReason
from .manager import RunManager
from .models import EventName, RunStatus
from .snapshots import DataSnapshotRecorder, SnapshotStore

try:
    from .synthesis import generate_executive_summary, save_executive_summary
except ImportError:  # Optional report-editor extension may be supplied by the UI layer.
    generate_executive_summary = None
    save_executive_summary = None

logger = logging.getLogger(__name__)

_ANALYSTS = {
    "market_report": ("Market Analyst", "market"),
    "sentiment_report": ("Sentiment Analyst", "social"),
    "news_report": ("News Analyst", "news"),
    "fundamentals_report": ("Fundamentals Analyst", "fundamentals"),
}
_SENSITIVE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^,\s}]+")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Map from a graph chunk key to the (display agent, analyst id, artifact
# type) tuple used to upsert the per-stage artifact row.
_ARTIFACT_KEYS: dict[str, tuple[str, str, str]] = {
    "market_report": ("Market Analyst", "market", "analyst_report"),
    "sentiment_report": ("Sentiment Analyst", "social", "analyst_report"),
    "news_report": ("News Analyst", "news", "analyst_report"),
    "fundamentals_report": ("Fundamentals Analyst", "fundamentals", "analyst_report"),
}


def _is_partial(content: str) -> bool:
    """A chunk value is treated as partial when it looks unfinished."""

    text = content.strip()
    if not text:
        return True
    tail = text[-3:]
    return tail in {"...", "..", " ?"} or text.endswith(("。", "，", ",", " "))


class WebRunRunner:
    """Execute one manager run and translate graph chunks into safe events."""

    def __init__(
        self,
        manager: RunManager,
        *,
        graph_factory: Callable[..., Any] = TradingAgentsGraph,
        config: dict[str, Any] | None = None,
        report_history: Any | None = None,
        artifact_repository: ArtifactRepository | None = None,
    ) -> None:
        self.manager = manager
        self.graph_factory = graph_factory
        self.config = config
        self.report_history = report_history
        self.artifact_repository = artifact_repository
        # Phase 2 defaults: 120s LLM timeout, 1 SDK retry, 300s LLM-op
        # budget, fallback to 60s for data sources.
        self.llm_timeout_seconds = float((config or {}).get("web_llm_timeout_seconds", 120.0))
        # Practical minimum: reasoning models (e.g. MiniMax-M3) commonly need
        # 30-60s once the prompt grows past ~30 KB of accumulated context, and
        # trading_graph uses this value as the per-call LLM ceiling. Anything
        # under ~180s will repeatedly abort Risk Management / Trading Team.
        self.llm_timeout_seconds = max(self.llm_timeout_seconds, 240.0)
        self.llm_max_retries = int((config or {}).get("web_llm_max_retries", 1))
        self.llm_op_budget_seconds = float((config or {}).get("web_llm_op_budget_seconds", 300.0))
        self.data_source_timeout_seconds = float(
            (config or {}).get("web_data_source_timeout_seconds", 60.0)
        )

    def worker(self, run_id: str) -> None:
        state = self.manager._state(run_id)  # guarded by manager methods below
        request = state.record.request
        cfg = copy.deepcopy(self.config if self.config is not None else DEFAULT_CONFIG)
        report_root = Path(cfg["results_dir"]) / "web_reports"
        self.manager.set_report_root(report_root)
        cfg["deadline_supplier"] = lambda: self.manager.remaining_deadline(run_id)
        # Bug fix: ``provider_timeout_seconds`` is consumed by
        # ``trading_graph.py`` as the LLM provider per-call ceiling. The
        # previous wiring fed in ``data_source_timeout_seconds`` (60s) which
        # starved reasoning-heavy providers (MiniMax-M3 routinely takes
        # 30-60s once accumulated context passes ~30 KB). LLM gets its own
        # value; data sources keep their timeout elsewhere.
        cfg["provider_timeout_seconds"] = float(self.llm_timeout_seconds)
        cfg.setdefault("web_llm_timeout_seconds", self.llm_timeout_seconds)
        cfg["llm_max_retries"] = self.llm_max_retries
        # Phase 2: opt into the per-ticker checkpoint saver. The resume
        # API relies on this to skip nodes that already completed.
        cfg["checkpoint_enabled"] = True
        # Environment overlays in DEFAULT_CONFIG win over the request's depth.
        if not os.environ.get("TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
            cfg["max_debate_rounds"] = request.research_depth
        if not os.environ.get("TRADINGAGENTS_MAX_RISK_ROUNDS"):
            cfg["max_risk_discuss_rounds"] = request.research_depth
        if request.output_language:
            cfg["output_language"] = request.output_language
        if request.provider:
            cfg["llm_provider"] = request.provider
        if request.quick_model:
            cfg["quick_think_llm"] = request.quick_model
        if request.deep_model:
            cfg["deep_think_llm"] = request.deep_model
        analysts = [getattr(a, "value", str(a)) for a in request.analysts]
        # Deterministic checkpoint signature so the manager can refuse a
        # retry that would silently use a different graph shape.
        signature = build_checkpoint_signature(
            ticker=request.ticker,
            analysis_date=str(request.analysis_date),
            asset_type=getattr(request.asset_type, "value", str(request.asset_type)),
            analysts=analysts,
            research_depth=request.research_depth,
            output_language=request.output_language,
            provider=request.provider,
            quick_model=request.quick_model,
            deep_model=request.deep_model,
            graph_version=GRAPH_VERSION,
        )
        cfg["checkpoint_signature"] = signature
        with suppress(KeyError):
            self.manager.set_checkpoint_retained_until(
                run_id,
                retained_until=retention_until().isoformat(),
                signature=signature,
            )
        # The external_request_checkpoint fires before each provider
        # call. Wrap it so we also tag the active operation / provider /
        # model and refresh activity timestamps. The lease thread keeps
        # the worker heartbeat alive independently.
        attempt_counter = {"value": 0}

        def _external_request_checkpoint() -> None:
            attempt_counter["value"] += 1
            with suppress(KeyError):
                self.manager.record_activity(
                    run_id,
                    operation="llm",
                    provider=request.provider,
                    model=request.deep_model,
                    attempt=attempt_counter["value"],
                )
            self.manager.heartbeat(run_id)

        cfg["external_request_checkpoint"] = _external_request_checkpoint
        self._heartbeat(run_id)
        graph = self.graph_factory(analysts, config=cfg, debug=False)
        phase = {"name": "Analyst Team", "index": 1, "phase_count": 5}
        analyst_statuses: dict[str, str] = {}
        completed_analysts: set[str] = set()
        self._publish_phase(run_id, phase, status="in_progress")
        self._publish_analyst_statuses(run_id, analysts, completed_analysts, analyst_statuses)
        self.manager.publish(
            run_id,
            EventName.PROGRESS,
            {"progress": 0.1, "phase": phase["name"], "current_agent": self._current_analyst(analysts, completed_analysts)},
        )

        def on_chunk(chunk: dict[str, Any]) -> None:
            self._heartbeat(run_id)
            self._publish_chunk(run_id, chunk, analysts, phase, completed_analysts, analyst_statuses)
            self._persist_chunk_artifacts(run_id, chunk, phase["name"])

        publishing = False
        try:
            result = graph.propagate(
                request.ticker,
                str(request.analysis_date),
                asset_type=getattr(request.asset_type, "value", str(request.asset_type)),
                on_chunk=on_chunk,
                should_cancel=lambda: self.manager.is_cancelled(run_id),
            )
            self._heartbeat(run_id)
            final_state, signal = result
            if self.manager.check_expired(run_id).status is not RunStatus.RUNNING:
                return
            if self.manager.is_cancelled(run_id):
                raise PropagationCancelled()
            report_id = run_id
            report_dir = (
                Path(cfg["results_dir"])
                / "web_reports"
                / safe_ticker_component(request.ticker)
                / str(request.analysis_date)
                / self._safe_run_id_component(run_id)
            )
            publishing_record = self.manager.begin_publishing(run_id)
            if publishing_record.status is not RunStatus.PUBLISHING:
                return
            publishing = True
            safe_run_id = self._safe_run_id_component(run_id)
            temporary_dir = report_dir.parent / ".tmp" / safe_run_id
            temporary_dir.parent.mkdir(parents=True, exist_ok=True)
            graph.save_reports(final_state, request.ticker, save_path=temporary_dir)
            snapshot_store = SnapshotStore(temporary_dir)
            snapshot_recorder = DataSnapshotRecorder(snapshot_store, safe_run_id, provider_chain=state.record.effective_quote_provider_chain)
            snapshot_recorder.record("analysis_state", final_state, provider=request.provider, request_fingerprint=f"{request.ticker}:{request.analysis_date}")
            manifest = snapshot_recorder.finalize()
            self.manager.set_data_metadata(
                run_id,
                data_snapshot_id=manifest["id"],
                data_status="complete",
                reproducibility="partial",
            )
            summary_status = "unavailable"
            try:
                deep_llm = getattr(graph, "deep_thinking_llm", None)
                if deep_llm is not None and generate_executive_summary is not None and save_executive_summary is not None:
                    summary = generate_executive_summary(
                        deep_llm,
                        ticker=request.ticker,
                        output_language=request.output_language or cfg.get("output_language", "English"),
                        final_state=final_state,
                    )
                    if summary:
                        save_executive_summary(temporary_dir, summary)
                        summary_status = "completed"
            except Exception:
                logger.warning("Executive summary generation failed for run %s", run_id, exc_info=True)
            (temporary_dir / "run.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "report_id": report_id,
                        "ticker": request.ticker,
                        "analysis_date": str(request.analysis_date),
                        "asset_type": getattr(request.asset_type, "value", str(request.asset_type)),
                        "analysts": analysts,
                        "research_depth": request.research_depth,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "signal": signal,
                        "status": "completed",
                        "provider": request.provider,
                        "quick_model": request.quick_model,
                        "deep_model": request.deep_model,
                        "output_language": request.output_language,
                        "summary_status": summary_status,
                        "quote_strategy_id": request.quote_strategy_id,
                        "effective_quote_strategy_id": request.quote_strategy_id,
                        "effective_quote_provider_chain": (["yfinance"] if request.quote_strategy_id == "default-yfinance" else ["yfinance", "alpha_vantage"] if request.quote_strategy_id == "fallback-yfinance-alpha-vantage" else []),
                        "data_snapshot_id": manifest["id"],
                        "data_status": "complete",
                        "reproducibility": "partial",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            committed = temporary_dir / "COMMITTED"
            committed.write_text("ok\n", encoding="utf-8")
            report_dir.parent.mkdir(parents=True, exist_ok=True)
            if report_dir.exists():
                raise FileExistsError("report already exists")
            temporary_dir.rename(report_dir)
            with suppress(OSError):
                temporary_dir.parent.rmdir()
            if self.report_history is not None:
                self.report_history.index_report(report_dir)
            self.manager.complete_publishing(
                run_id,
                signal=signal,
                report_id=report_id,
                report_dir=report_dir,
            )
        except PropagationCancelled:
            self.manager.cancel_run(run_id, phase=phase["name"])
        except Exception as exc:
            logger.error("Web analysis failed for run %s\n%s", run_id, traceback.format_exc())
            # The active_operation/active_provider/active_attempt fields
            # captured the most recent LLM/data call. Surface them in
            # the terminal record so the user sees exactly which model
            # and provider failed.
            info = classify_provider_exception(
                exc,
                provider=state.record.active_provider,
                model=state.record.active_model,
                phase=phase["name"],
                agent=state.record.current_agent,
                attempt=state.record.active_attempt,
                operation=state.record.active_operation,
            )
            if publishing:
                self.manager.fail_run(
                    run_id,
                    error_code=TerminalReason.PUBLISH_INCOMPLETE.value,
                    error_message=USER_MESSAGES[TerminalReason.PUBLISH_INCOMPLETE.value],
                    failed_phase=phase["name"],
                    failed_provider=info.provider,
                    failed_model=info.model,
                    retryable=False,
                )
            else:
                self.manager.fail_run_classified(run_id, info)
            with suppress(KeyError):
                self.manager.clear_activity(run_id)

    def _heartbeat(self, run_id: str) -> None:
        self.manager.heartbeat(run_id)

    def _persist_chunk_artifacts(
        self,
        run_id: str,
        chunk: dict[str, Any],
        current_phase: str,
    ) -> None:
        """Idempotent upsert of per-stage artifact rows from one chunk.

        Only the chunk keys declared in :data:`_ARTIFACT_KEYS` produce
        artifacts; research debates, trader plans, and risk debates are
        derived at publishing time so they don't need partial coverage.
        """

        if self.artifact_repository is None:
            return
        sequence = 0
        for key, (agent, _analyst_key, artifact_type) in _ARTIFACT_KEYS.items():
            value = chunk.get(key)
            if not value:
                continue
            content = str(value)
            status = "partial" if _is_partial(content) else "completed"
            try:
                self.artifact_repository.upsert(
                    run_id,
                    artifact_key=key,
                    artifact_type=artifact_type,
                    phase=current_phase,
                    agent=agent,
                    title=f"{agent} report",
                    content_markdown=content,
                    status=status,
                    sequence=sequence,
                )
            except Exception:
                logger.exception("artifact upsert failed for run %s key=%s", run_id, key)
            sequence += 1

    def _publish_chunk(
        self,
        run_id: str,
        chunk: dict[str, Any],
        analysts: list[str],
        phase: dict[str, Any],
        completed_analysts: set[str] | None = None,
        analyst_statuses: dict[str, str] | None = None,
    ) -> None:
        self._heartbeat(run_id)
        completed_analysts = completed_analysts if completed_analysts is not None else set()
        analyst_statuses = analyst_statuses if analyst_statuses is not None else {}
        for key, (agent, analyst_key) in _ANALYSTS.items():
            if chunk.get(key):
                if analyst_key in analysts:
                    completed_analysts.add(analyst_key)
                self.manager.publish(run_id, EventName.AGENT_STATUS, {"agent": agent, "status": "completed"})
                self.manager.publish(run_id, EventName.MESSAGE, {"message_type": "report", "text": self._shorten(chunk[key])})
        self._publish_analyst_statuses(run_id, analysts, completed_analysts, analyst_statuses)
        if analysts:
            progress = 0.1 + 0.3 * len(completed_analysts) / len(analysts)
            self.manager.publish(
                run_id,
                EventName.PROGRESS,
                {"progress": progress, "phase": phase["name"], "current_agent": self._current_analyst(analysts, completed_analysts)},
            )
        if chunk.get("investment_debate_state"):
            debate = chunk["investment_debate_state"]
            phase.update(name="Research Team", index=2)
            self._publish_phase(run_id, phase, status="in_progress")
            self.manager.publish(run_id, EventName.PROGRESS, {"progress": 0.45, "phase": phase["name"], "current_agent": "Bull Researcher"})
            for agent, key in (("Bull Researcher", "bull_history"), ("Bear Researcher", "bear_history"), ("Research Manager", "judge_decision")):
                if str(debate.get(key, "")).strip():
                    self.manager.publish(run_id, EventName.AGENT_STATUS, {"agent": agent, "status": "in_progress" if key != "judge_decision" else "completed"})
        if chunk.get("trader_investment_plan"):
            phase.update(name="Trading Team", index=3)
            self._publish_phase(run_id, phase, status="in_progress")
            self.manager.publish(run_id, EventName.PROGRESS, {"progress": 0.65, "phase": phase["name"], "current_agent": "Trader"})
            self.manager.publish(run_id, EventName.AGENT_STATUS, {"agent": "Trader", "status": "completed"})
        if chunk.get("risk_debate_state"):
            risk = chunk["risk_debate_state"]
            phase.update(name="Risk Management", index=4)
            self._publish_phase(run_id, phase, status="in_progress")
            self.manager.publish(run_id, EventName.PROGRESS, {"progress": 0.8, "phase": phase["name"], "current_agent": "Aggressive Analyst"})
            for agent, key in (("Aggressive Analyst", "aggressive_history"), ("Conservative Analyst", "conservative_history"), ("Neutral Analyst", "neutral_history"), ("Portfolio Manager", "judge_decision")):
                if str(risk.get(key, "")).strip():
                    self.manager.publish(run_id, EventName.AGENT_STATUS, {"agent": agent, "status": "completed" if key == "judge_decision" else "in_progress"})
        if not any(k in chunk for k in (*_ANALYSTS, "investment_debate_state", "trader_investment_plan", "risk_debate_state")):
            self.manager.publish(run_id, EventName.ACTIVITY, {"activity_type": "graph_update", "name": "graph", "summary": self._shorten(chunk)})

    def _publish_phase(self, run_id: str, phase: dict[str, Any], *, status: str) -> None:
        self._heartbeat(run_id)
        self.manager.publish(
            run_id,
            EventName.PHASE_CHANGED,
            {
                "phase": phase["name"],
                "phase_index": phase["index"],
                "phase_count": phase.get("phase_count", 5),
                "status": status,
            },
        )

    def _publish_analyst_statuses(
        self,
        run_id: str,
        analysts: list[str],
        completed: set[str],
        previous: dict[str, str],
    ) -> None:
        self._heartbeat(run_id)
        active_assigned = False
        for _analyst_key, (agent, selected_key) in _ANALYSTS.items():
            if selected_key not in analysts:
                continue
            if selected_key in completed:
                status = "completed"
            elif not active_assigned:
                status = "in_progress"
                active_assigned = True
            else:
                status = "pending"
            if previous.get(agent) != status:
                self.manager.publish(run_id, EventName.AGENT_STATUS, {"agent": agent, "status": status})
                previous[agent] = status

    @staticmethod
    def _current_analyst(analysts: list[str], completed: set[str]) -> str | None:
        for _key, (agent, selected_key) in _ANALYSTS.items():
            if selected_key in analysts and selected_key not in completed:
                return agent
        return None

    @staticmethod
    def _safe_run_id_component(run_id: str) -> str:
        if _SAFE_RUN_ID.fullmatch(run_id):
            return run_id
        digest = hashlib.sha256(run_id.encode("utf-8", "surrogatepass")).hexdigest()[:32]
        return f"run-{digest}"

    @staticmethod
    def _shorten(value: Any, limit: int = 240) -> str:
        text = _SENSITIVE.sub(r"\1=[redacted]", str(value)).replace("\n", " ")
        return text if len(text) <= limit else text[: limit - 3] + "..."


def run_web_analysis(manager: RunManager, run_id: str, **kwargs: Any) -> None:
    """Run a manager-owned analysis synchronously (useful for tests)."""
    WebRunRunner(manager, **kwargs).worker(run_id)
