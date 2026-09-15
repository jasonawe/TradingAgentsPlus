"""Q7 / P2-10 — SurfaceOp namespace (roadmap §11.4 P2-2 + dsh §10.1.3).

The EventLog already supports ``replace()`` and ``chat_history(apply_replaces=True)``
mechanically, but the *intent* is scattered:
  - system prompt replace lives in ``EventLog.commit_system_prompt()``
  - raw replace lives in ``EventLog.replace(seq, new_data)``
  - LLM context derivation lives in ``EventLog.chat_history(apply_replaces=True)``
  - users wanting to "redact PII from message N" have to know the
    internal seq, the replace tuple shape, AND the chat_history flag.

``SurfaceOp`` is the explicit, named namespace dsh uses for "append /
replace / undo" operations on a session's surface chain. We mirror it
so plugin / app code can do::

    from tradingagents.agent_harness.memory.surface_op import SurfaceOp
    SurfaceOp(event_log).replace_message(session_id, seq, new_content, reason="redact PII")
    history = SurfaceOp(event_log).derive_history(session_id, apply_replaces=True)

…and never poke ``EventLog.replace`` / ``chat_history`` directly.

Why a class wrapper instead of module-level functions?
  - Bundles the ``EventLog`` reference so callers don't have to pass it.
  - Centralises the "what's replaceable" rules — e.g. we forbid
    replacing audit-only events (replacing a write_audit_log row
    would defeat the audit trail).
  - Easier to add new operations (insert_before, swap, fork) without
    bloating the ``EventLog`` API.

Notes:
  - This is purely a *facade* — the underlying ``EventLog.replace``
    mechanics are unchanged, so existing tests / migration code keeps
    working.
"""
from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

# Event types that flow into the LLM context (i.e. can be replaced via
# SurfaceOp without breaking audit / replay semantics). Audit-only events
# such as ``write_audit_log`` and ``archive/legacy_history`` are excluded.
REPLACEABLE_TYPES: frozenset[str] = frozenset({
    "system/message",
    "user/message",
    "assistant/message",
    "tool/result",
    "tool/call",
})


class SurfaceOpError(RuntimeError):
    """Raised when a SurfaceOp cannot be applied."""


class SurfaceOp:
    """Namespace for append / replace / undo operations on a session's
    surface chain.
    """

    def __init__(self, event_log: Any) -> None:
        self._log = event_log

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def derive_history(
        self, session_id: str, *, apply_replaces: bool = True,
    ) -> list[dict[str, Any]]:
        """Project the full surface chain into LLM-shaped messages.

        Thin wrapper around :meth:`EventLog.chat_history` so callers
        don't have to remember the ``apply_replaces`` flag.
        """
        return self._log.chat_history(
            session_id, apply_replaces=apply_replaces,
        )

    def get_event(self, session_id: str, seq: int) -> Any | None:
        """Return the underlying ``Event`` for a (session, seq) pair."""
        return self._log.get(session_id, seq)

    def surface_events(self, session_id: str) -> list[Any]:
        """Return surface-classified events in seq order."""
        return list(self._log.surface_events(session_id))

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def commit_system_prompt(
        self, session_id: str, prompt: str, *, ts: float | None = None,
    ) -> Any:
        """Idempotent: replace the existing system prompt if any."""
        return self._log.commit_system_prompt(session_id, prompt, ts=ts)

    def append(
        self, session_id: str, type_: str, data: Any,
        *, ts: float | None = None,
    ) -> Any:
        """Append a new surface event to ``session_id``.

        ``ts`` is forwarded to ``EventLog.append`` for deterministic
        test ordering; pass ``None`` to use the wall-clock default.
        """
        return self._log.append(session_id, type_, data, ts=ts)

    # ------------------------------------------------------------------
    # Replace (the heart of Q7)
    # ------------------------------------------------------------------
    def replace_message(
        self,
        session_id: str,
        seq: int,
        new_content: Any,
        *,
        reason: str | None = None,
        fields: tuple[str, ...] = ("content",),
    ) -> Any:
        """Replace the ``content`` (or other named fields) of a surface event.

        Args:
            session_id: owning session.
            seq: target event seq.
            new_content: new value for the named fields. For a
                ``user/message`` event this is typically a plain string;
                for ``tool/result`` it's the corrected result object.
            reason: free-form audit reason (e.g. "redact PII",
                "user rephrased", "tool output corrected").
            fields: which keys to update inside ``event.data``. Defaults
                to ``("content",)`` because that's the field the LLM
                actually consumes.

        Returns the new replace event appended to the log.

        Raises:
            SurfaceOpError: target event missing, not replaceable, or
                ``new_content`` invalid.
        """
        original = self._log.get(session_id, seq)
        if original is None:
            raise SurfaceOpError(f"event ({session_id}, {seq}) not found")
        if original.type not in REPLACEABLE_TYPES:
            raise SurfaceOpError(
                f"event type {original.type!r} is not replaceable; "
                f"allowed: {sorted(REPLACEABLE_TYPES)}"
            )
        # Build the new data dict: start from the original data, patch
        # the named fields with new_content (which may be scalar or dict).
        new_data = dict(original.data) if isinstance(original.data, dict) else {"content": original.data}
        if isinstance(new_content, dict):
            for k, v in new_content.items():
                if k in fields:
                    new_data[k] = v
        else:
            # scalar / single-field mode
            for f in fields:
                new_data[f] = new_content
        return self._log.replace(
            session_id, seq, new_data, reason=reason,
        )

    def replace_field(
        self,
        session_id: str,
        seq: int,
        field: str,
        new_value: Any,
        *,
        reason: str | None = None,
    ) -> Any:
        """Convenience: replace a single field of an event."""
        return self.replace_message(
            session_id, seq, new_content={field: new_value},
            reason=reason, fields=(field,),
        )

    def undo_replace(self, session_id: str, replace_seq: int) -> Any:
        """Undo a previous replace by appending another replace that
        restores the original ``new_data`` to the *previous* value.

        The audit chain is::

            event(N)  →  replace_event(A)  →  replace_event(B) [undo A]

        and ``chat_history(apply_replaces=True)`` follows the chain
        forwards, so the latest replace wins.
        """
        replace_event = self._log.get(session_id, replace_seq)
        if replace_event is None or replace_event.type != "replace":
            raise SurfaceOpError(
                f"event ({session_id}, {replace_seq}) is not a replace event"
            )
        target_seq = (replace_event.data or {}).get("target_seq")
        if target_seq is None:
            raise SurfaceOpError(
                f"replace event {replace_seq} has no target_seq"
            )
        # The original value lives on the previous event in the chain
        # for ``target_seq`` — i.e. the event just before replace_seq
        # in the same chain. Simpler: read the current head event for
        # target_seq and use ITS data as the new value, so we toggle
        # back. (For a richer undo history, callers can walk the
        # ``replaced_by`` chain themselves.)
        original = self._log.get(session_id, target_seq)
        if original is None:
            raise SurfaceOpError(
                f"target event ({session_id}, {target_seq}) not found"
            )
        # If there's a previous replace, walk back to its new_data.
        prev = original.replaced_by
        if prev is not None and prev != replace_seq:
            prev_event = self._log.get(session_id, prev)
            if prev_event is not None and isinstance(prev_event.data, dict):
                restore_data = prev_event.data.get("new_data", original.data)
            else:
                restore_data = original.data
        else:
            restore_data = original.data
        return self._log.replace(
            session_id, target_seq, restore_data,
            reason=f"undo replace {replace_seq}",
        )


__all__ = [
    "SurfaceOp",
    "SurfaceOpError",
    "REPLACEABLE_TYPES",
]
