"""Step 42 — Workflow ↔ Checkpoint mapping.

Each ``Workflow.run()`` execution writes checkpoints at every node
boundary. The composite key is ``(session_id, workflow_name,
milestone_id)`` so multiple workflows can share a session's history
without overwriting each other.

Resume semantics
----------------
``last_node_completed(session_id, workflow_name)`` returns the
node position of the most recently completed checkpoint, which the
Workflow can use as the entry point on resume (skipping already-
done nodes).

Used by:
- ``Workflow.run()`` — calls ``_maybe_checkpoint(state, node_id)``
  after every node finishes
- Test harness — verifies checkpoint↔workflow invariants
- ``/api/harness/sessions/{sid}/resume`` — picks the right entry
  node based on the last checkpoint for the workflow being resumed
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger(__name__)


# Default workflow_name when none is supplied (back-compat with rows
# written before the 017 migration).
DEFAULT_WORKFLOW_NAME = "default"


@dataclass
class WorkflowCheckpoint:
    """Single (workflow, session, milestone) checkpoint record."""

    session_id: str
    workflow_name: str
    milestone_id: str
    node_position: str
    state: dict[str, Any]
    emitted_events: list[tuple[str, dict[str, Any]]]


def workflow_checkpoint_key(
    session_id: str,
    workflow_name: str,
    milestone_id: str,
) -> tuple[str, str, str]:
    """Canonical (session, workflow, milestone) composite key."""
    return (session_id, workflow_name or DEFAULT_WORKFLOW_NAME, milestone_id)


def make_milestone_id(workflow_name: str, node_id: str, counter: int) -> str:
    """Generate a stable milestone id from (workflow, node, counter)."""
    return f"{workflow_name}:{node_id}:{counter}"


def last_node_completed_from_rows(
    rows: list[dict[str, Any]],
) -> str | None:
    """Given checkpoint rows for one (session, workflow), return the
    node_position of the most recently completed one (highest counter).
    """
    if not rows:
        return None
    # Pick the row with the largest trailing-counter in milestone_id.
    def _counter(row: dict[str, Any]) -> int:
        mid = row.get("milestone_id", "")
        try:
            return int(mid.rsplit(":", 1)[-1])
        except (ValueError, IndexError):
            return -1
    best = max(rows, key=_counter)
    return best.get("node_position")


__all__ = [
    "WorkflowCheckpoint",
    "DEFAULT_WORKFLOW_NAME",
    "workflow_checkpoint_key",
    "make_milestone_id",
    "last_node_completed_from_rows",
]
