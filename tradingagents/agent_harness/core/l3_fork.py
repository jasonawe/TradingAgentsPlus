"""Step 28 — fork a session's L3 discussion reference into a new session.

When the user starts a new session and wants to continue a previous
conversation, ``fork_session_reference`` copies the source session's
``discussions:<source_sid>`` row to ``discussions:<target_sid>`` so
the next synthesize call has the prior context to surface.

The orchestrator's ``_build_l3_reference_block`` (Step 21) reads
``discussions:`` entries by ``state.session_id`` — without a fork
the new session would have no row to surface until it ran a turn
on its own.
"""
from __future__ import annotations

from typing import Any


def fork_session_reference(
    l3: Any,
    *,
    source_session_id: str,
    target_session_id: str,
    ttl_seconds: int = 7 * 86400,
) -> bool:
    """Copy the source session's L3 discussion record to ``target_session_id``.

    Returns True if a row was copied, False if the source had nothing
    to fork (or the L3 layer rejected the write).
    """
    if not source_session_id or not target_session_id:
        return False
    try:
        src_entry = l3.get(f"discussions:{source_session_id}")
    except Exception:
        return False
    if src_entry is None:
        return False
    # Build the new payload. We keep the source value intact but
    # record ``forked_from`` so the prompt can render "this session
    # was forked from <source>".
    payload = dict(src_entry.value or {})
    payload["forked_from"] = source_session_id
    try:
        l3.set(
            f"discussions:{target_session_id}",
            payload,
            session_id=target_session_id,
            ttl_seconds=ttl_seconds,
        )
        return True
    except Exception:
        return False
