"""§P3-5 — chain walker for the "基于报告再分析" feature.

Each report carries ``based_on_report_id`` (the prior report id it
was anchored on, or ``None`` for a standalone run). Walking the
chain backwards — current → its prior → that prior's prior → ... —
gives the full lineage, which the UI surfaces as a "对比前次"
panel near the top of the report detail page and as a chain link
in the report library list.

Failure modes the walker handles gracefully:
- Cycle (A → B → A). Capped at ``_MAX_DEPTH`` and visited-set
  prevents infinite recursion.
- Missing prior report (deleted / corrupted). The walker stops
  at the first unresolvable id rather than raising.
- Cross-ticker chain (prior was a different symbol — shouldn't
  happen because based_on_report_id is only set by the runner
  for the same ticker, but the walker still emits whatever it
  finds so the UI can show "prior was for X" if it does).

Output shape
------------
``walk_report_chain(report_id)`` returns an ordered list starting
with the requested report and walking backwards::

    [
        {"report_id": "run-current", "ticker": "600036.SS",
         "signal": "Overweight", "based_on_report_id": "run-prior"},
        {"report_id": "run-prior",   "ticker": "600036.SS",
         "signal": "Hold",         "based_on_report_id": None},
        ...
    ]

``resolve_prior_summary(report_id)`` returns just the immediate
prior's signal/date/summary — used by the "对比前次" panel in the
report detail UI.
"""
from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

_MAX_DEPTH = 10


def _safe_get(history: Any, report_id: str) -> dict[str, Any] | None:
    try:
        record = history.get_report(report_id)
    except Exception as e:
        LOGGER.debug("walk_report_chain: get_report(%s) failed: %s", report_id, e)
        return None
    return record if isinstance(record, dict) else None


def walk_report_chain(
    history: Any,
    report_id: str,
    *,
    max_depth: int = _MAX_DEPTH,
) -> list[dict[str, Any]]:
    """Return the lineage for ``report_id``, newest first.

    Stops when:
    - ``based_on_report_id`` is None (chain root),
    - a prior id can't be resolved (deleted / missing),
    - the same id is seen twice (cycle safety),
    - ``max_depth`` is reached.

    Each element is a small dict with the fields the UI cares
    about (``report_id``, ``ticker``, ``signal``, ``analysis_date``,
    ``based_on_report_id``). The full report body is intentionally
    omitted — callers that need it can hit ``get_report(id)``.
    """
    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    current_id: str | None = report_id
    depth = 0
    while current_id and depth < max_depth:
        if current_id in visited:
            LOGGER.debug("walk_report_chain: cycle at %s, stopping", current_id)
            break
        visited.add(current_id)
        record = _safe_get(history, current_id)
        if record is None:
            break
        chain.append(
            {
                "report_id": record.get("report_id") or current_id,
                "ticker": record.get("ticker") or "",
                "signal": record.get("signal") or "",
                "rating": record.get("rating") or "",
                "analysis_date": record.get("analysis_date") or "",
                "generated_at": record.get("generated_at") or "",
                "based_on_report_id": record.get("based_on_report_id"),
            }
        )
        current_id = record.get("based_on_report_id")
        depth += 1
    return chain


def resolve_prior_summary(
    history: Any,
    report_id: str,
) -> dict[str, Any] | None:
    """Return a single-element dict describing the immediate prior.

    Returns ``None`` when the report has no prior link or the
    prior can't be resolved. Used by the report detail UI to
    render the "对比前次" panel without walking the full chain.
    """
    record = _safe_get(history, report_id)
    if record is None:
        return None
    prior_id = record.get("based_on_report_id")
    if not prior_id:
        return None
    prior = _safe_get(history, prior_id)
    if prior is None:
        # The current report claims a prior but the prior is gone.
        # Surface that fact so the UI can show "前次报告缺失".
        return {
            "report_id": prior_id,
            "missing": True,
        }
    return {
        "report_id": prior.get("report_id") or prior_id,
        "ticker": prior.get("ticker") or "",
        "signal": prior.get("signal") or "",
        "rating": prior.get("rating") or "",
        "analysis_date": prior.get("analysis_date") or "",
        "generated_at": prior.get("generated_at") or "",
        "summary": (prior.get("complete_report") or "")[:1200],
        "missing": False,
    }
