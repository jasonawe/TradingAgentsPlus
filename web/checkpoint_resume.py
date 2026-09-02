"""Helpers for the Phase-2 retry-from-checkpoint flow.

Wraps :mod:`tradingagents.graph.checkpointer` with:

* a deterministic checkpoint signature that captures every input that
  can change graph shape or prompt output (analyst selection, debate /
  risk depth, output language, providers, models, asset mode, plus a
  declared graph version);
* the retention policy that keeps checkpoints for non-completed runs
  for a configurable TTL before garbage collection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tradingagents.graph.checkpointer import (
    checkpoint_step,
    clear_checkpoint,
    get_checkpointer,
    thread_id as _thread_id,
)

GRAPH_VERSION = "tradingagents-plus@1"


def build_checkpoint_signature(
    *,
    ticker: str,
    analysis_date: str,
    asset_type: str,
    analysts: list[str],
    research_depth: int,
    output_language: str | None,
    provider: str | None,
    quick_model: str | None,
    deep_model: str | None,
    graph_version: str = GRAPH_VERSION,
) -> str:
    """Build a stable signature string for the run's graph-affecting inputs."""

    parts = [
        "graph=" + graph_version,
        "ticker=" + ticker.upper(),
        "date=" + analysis_date,
        "asset=" + asset_type,
        "analysts=" + ",".join(sorted(analysts)),
        "depth=" + str(research_depth),
        "lang=" + (output_language or ""),
        "provider=" + (provider or ""),
        "quick=" + (quick_model or ""),
        "deep=" + (deep_model or ""),
    ]
    return "|".join(parts)


def thread_id_for_run(signature: str) -> str:
    return _thread_id("", "", signature)


def checkpoint_id(signature: str) -> str:
    return thread_id_for_run(signature)


def checkpoint_available(
    data_dir: str | Path,
    ticker: str,
    analysis_date: str,
    signature: str,
) -> bool:
    """Return True if a resumable checkpoint exists for the given signature."""

    return checkpoint_step(
        data_dir,
        ticker,
        analysis_date,
        signature=signature,
    ) is not None


def checkpoint_step_value(
    data_dir: str | Path,
    ticker: str,
    analysis_date: str,
    signature: str,
) -> int | None:
    return checkpoint_step(data_dir, ticker, analysis_date, signature=signature)


def clear_checkpoint_for_signature(
    data_dir: str | Path,
    ticker: str,
    analysis_date: str,
    signature: str,
) -> None:
    clear_checkpoint(data_dir, ticker, analysis_date, signature=signature)


def checkpoint_metadata_for_signature(
    data_dir: str | Path,
    ticker: str,
    analysis_date: str,
    signature: str,
) -> dict[str, Any] | None:
    """Return the LangGraph metadata dict for the signature's checkpoint, or None."""

    db = Path(data_dir) / "checkpoints" / f"{ticker.upper()}.db"
    if not db.exists():
        return None
    tid = thread_id_for_run(signature)
    with get_checkpointer(data_dir, ticker) as saver:
        cp = saver.get_tuple({"configurable": {"thread_id": tid}})
    if cp is None:
        return None
    return dict(cp.metadata or {})


def retention_until(
    *,
    now: datetime | None = None,
    ttl_days: int = 7,
) -> datetime:
    reference = now or datetime.now(timezone.utc)
    return reference + timedelta(days=ttl_days)


def is_retained(retained_until: datetime | None, *, now: datetime | None = None) -> bool:
    if retained_until is None:
        return False
    reference = now or datetime.now(timezone.utc)
    return retained_until > reference


__all__ = [
    "GRAPH_VERSION",
    "build_checkpoint_signature",
    "checkpoint_available",
    "checkpoint_id",
    "checkpoint_metadata_for_signature",
    "checkpoint_step_value",
    "clear_checkpoint_for_signature",
    "is_retained",
    "retention_until",
    "thread_id_for_run",
]
