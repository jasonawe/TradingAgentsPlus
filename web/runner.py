"""Graph execution adapter for web run manager events."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.propagation import PropagationCancelled
from tradingagents.graph.trading_graph import TradingAgentsGraph

from .manager import RunManager
from .models import EventName

logger = logging.getLogger(__name__)

_ANALYSTS = {
    "market_report": ("Market Analyst", "market"),
    "sentiment_report": ("Sentiment Analyst", "social"),
    "news_report": ("News Analyst", "news"),
    "fundamentals_report": ("Fundamentals Analyst", "fundamentals"),
}
_SENSITIVE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^,\s}]+")


class WebRunRunner:
    """Execute one manager run and translate graph chunks into safe events."""

    def __init__(
        self,
        manager: RunManager,
        *,
        graph_factory: Callable[..., Any] = TradingAgentsGraph,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.manager = manager
        self.graph_factory = graph_factory
        self.config = config

    def worker(self, run_id: str) -> None:
        state = self.manager._state(run_id)  # guarded by manager methods below
        request = state.record.request
        cfg = copy.deepcopy(self.config if self.config is not None else DEFAULT_CONFIG)
        # Environment overlays in DEFAULT_CONFIG win over the request's depth.
        if not os.environ.get("TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
            cfg["max_debate_rounds"] = request.research_depth
        if not os.environ.get("TRADINGAGENTS_MAX_RISK_ROUNDS"):
            cfg["max_risk_discuss_rounds"] = request.research_depth
        analysts = [getattr(a, "value", str(a)) for a in request.analysts]
        graph = self.graph_factory(analysts, config=cfg, debug=False)
        phase = {"name": "Analyst Team", "index": 1}

        def on_chunk(chunk: dict[str, Any]) -> None:
            self._publish_chunk(run_id, chunk, analysts, phase)

        try:
            result = graph.propagate(
                request.ticker,
                str(request.analysis_date),
                asset_type=getattr(request.asset_type, "value", str(request.asset_type)),
                on_chunk=on_chunk,
                should_cancel=lambda: self.manager.is_cancelled(run_id),
            )
            final_state, signal = result
            if self.manager.is_cancelled(run_id):
                raise PropagationCancelled()
            report_id = run_id
            report_dir = (
                Path(cfg["results_dir"])
                / "web_reports"
                / safe_ticker_component(request.ticker)
                / str(request.analysis_date)
                / run_id
            )
            graph.save_reports(final_state, request.ticker, save_path=report_dir)
            (report_dir / "run.json").write_text(
                json.dumps({"run_id": run_id, "report_id": report_id}, indent=2),
                encoding="utf-8",
            )
            self.manager.complete_run(run_id, signal=signal, report_id=report_id)
        except PropagationCancelled:
            self.manager.cancel_run(run_id, phase=phase["name"])
        except Exception:
            logger.error("Web analysis failed for run %s\n%s", run_id, traceback.format_exc())
            self.manager.fail_run(
                run_id,
                error_code="worker_error",
                error_message="analysis worker failed",
            )

    def _publish_chunk(self, run_id: str, chunk: dict[str, Any], analysts: list[str], phase: dict[str, Any]) -> None:
        for key, (agent, _analyst_key) in _ANALYSTS.items():
            if chunk.get(key):
                self.manager.publish(run_id, EventName.AGENT_STATUS, {"agent": agent, "status": "completed"})
                self.manager.publish(run_id, EventName.MESSAGE, {"message_type": "report", "text": self._shorten(chunk[key])})
        if chunk.get("investment_debate_state"):
            debate = chunk["investment_debate_state"]
            phase.update(name="Research Team", index=2)
            self.manager.publish(run_id, EventName.PHASE_CHANGED, {"phase": phase["name"], "phase_index": 2, "phase_count": 5, "status": "in_progress"})
            for agent, key in (("Bull Researcher", "bull_history"), ("Bear Researcher", "bear_history"), ("Research Manager", "judge_decision")):
                if str(debate.get(key, "")).strip():
                    self.manager.publish(run_id, EventName.AGENT_STATUS, {"agent": agent, "status": "in_progress" if key != "judge_decision" else "completed"})
        if chunk.get("trader_investment_plan"):
            self.manager.publish(run_id, EventName.PHASE_CHANGED, {"phase": "Trading Team", "phase_index": 3, "phase_count": 5, "status": "in_progress"})
            self.manager.publish(run_id, EventName.AGENT_STATUS, {"agent": "Trader", "status": "completed"})
        if chunk.get("risk_debate_state"):
            risk = chunk["risk_debate_state"]
            self.manager.publish(run_id, EventName.PHASE_CHANGED, {"phase": "Risk Management", "phase_index": 4, "phase_count": 5, "status": "in_progress"})
            for agent, key in (("Aggressive Analyst", "aggressive_history"), ("Conservative Analyst", "conservative_history"), ("Neutral Analyst", "neutral_history"), ("Portfolio Manager", "judge_decision")):
                if str(risk.get(key, "")).strip():
                    self.manager.publish(run_id, EventName.AGENT_STATUS, {"agent": agent, "status": "completed" if key == "judge_decision" else "in_progress"})
        if not any(k in chunk for k in (*_ANALYSTS, "investment_debate_state", "trader_investment_plan", "risk_debate_state")):
            self.manager.publish(run_id, EventName.ACTIVITY, {"activity_type": "graph_update", "name": "graph", "summary": self._shorten(chunk)})

    @staticmethod
    def _shorten(value: Any, limit: int = 240) -> str:
        text = _SENSITIVE.sub(r"\1=[redacted]", str(value)).replace("\n", " ")
        return text if len(text) <= limit else text[: limit - 3] + "..."


def run_web_analysis(manager: RunManager, run_id: str, **kwargs: Any) -> None:
    """Run a manager-owned analysis synchronously (useful for tests)."""
    WebRunRunner(manager, **kwargs).worker(run_id)
