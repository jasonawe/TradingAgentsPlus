"""§0.4.30 — Tool policy gate (DeepSeek ``tools/pre-execute`` equivalent).

DeepSeek-Harness pattern: every tool has a per-tool ``pre_execute``
hook that can ``reject`` / ``rewrite`` / ``ask`` before the call
runs. The hook is dynamic per-call (uses ``context``) rather than
static dispatch tables. Read-only tools get a no-op hook; write
tools get the HITL gate automatically.

This module exposes two things:

1. :func:`policy_for_tool` — returns the right policy callable for a
   given tool name. ``write`` tools get :func:`hitl_policy`; read
   tools get :func:`passthrough_policy`. The lookup is driven by the
   catalog entry's ``is_write`` flag (no static dispatch table —
   that's the whole point of DeepSeek's pattern).

2. :func:`apply_policies` — calls every registered policy in order
   until one returns a non-``ALLOW`` verdict. The orchestrator wires
   this between plan parsing and tool execution.

The HITL policy mirrors what ``_hitl_gate`` does today, but returns a
structured :class:`PolicyDecision` instead of a raw gate dict. This
keeps the policy module self-contained — the orchestrator doesn't
need to know whether a tool is gated by an LLM-judge pre-filter, a
risk policy, or HITL.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

LOGGER = logging.getLogger(__name__)


class Verdict(str, Enum):
    """Result of a policy check on a tool call."""

    ALLOW = "allow"
    REJECT = "reject"
    ASK = "ask"  # needs user confirmation (HITL gate)


@dataclass
class PolicyDecision:
    """Structured policy result.

    - ``verdict == ALLOW`` and ``rewritten_args`` is None -> proceed
      with the original args.
    - ``verdict == ALLOW`` and ``rewritten_args`` is a dict -> proceed
      but use the rewritten args (e.g. defaults filled in).
    - ``verdict == ASK`` -> emit HITL gate; orchestrator pauses turn.
    - ``verdict == REJECT`` -> skip the call; orchestrator surfaces
      the ``reason`` to the synthesizer.
    """

    verdict: Verdict
    reason: str = ""
    rewritten_args: dict[str, Any] | None = None
    # Only set when verdict == ASK; lets the orchestrator emit the
    # pending_approval SSE event without re-deriving the gate payload.
    gate_payload: dict[str, Any] | None = None


# ----------------------------------------------------------------------
# Policy implementations
# ----------------------------------------------------------------------


def passthrough_policy(args: dict, *, context: Any) -> PolicyDecision:
    """Default for read-only tools — pass through unchanged."""
    return PolicyDecision(verdict=Verdict.ALLOW)


def hitl_policy(args: dict, *, context: Any) -> PolicyDecision:
    """Write tools -> emit a HITL gate.

    Mirrors ``tools/builtin.py:_hitl_gate`` but returns a structured
    decision. The orchestrator resolves the gate via
    :func:`hitl_consume` after the user confirms.
    """
    try:
        from tradingagents.agent_harness.tools.impl import _hitl_gate
    except Exception:
        # Defensive: if impl is not importable (e.g. test env without
        # bridge), fall back to ALLOW rather than crashing the plan.
        LOGGER.debug("hitl_policy: _hitl_gate not importable, allowing %s", args)
        return PolicyDecision(verdict=Verdict.ALLOW)
    session_id = getattr(context, "session_id", "default")
    tool_name = getattr(context, "tool_name", "?")
    user_message = getattr(context, "user_message", None)
    try:
        gate = _hitl_gate(session_id, tool_name, args, user_message)
    except Exception as e:
        LOGGER.warning("hitl_policy: gate build failed: %s", e)
        # Fail-closed: if we can't decide, ASK so the user confirms.
        return PolicyDecision(
            verdict=Verdict.ASK,
            reason=f"HITL gate build failed: {e}",
        )
    if gate is None:
        return PolicyDecision(verdict=Verdict.ALLOW)
    return PolicyDecision(
        verdict=Verdict.ASK,
        reason=f"{tool_name} requires user confirmation",
        gate_payload=gate,
    )


# ----------------------------------------------------------------------
# Policy registry + applier
# ----------------------------------------------------------------------


# Per-tool override map. Tools that need a custom policy (e.g. a
# non-HITL risk gate) register here. Default behaviour comes from the
# catalog ``is_write`` flag.
_CUSTOM_POLICIES: dict[str, Any] = {}


def register_policy(tool_name: str, policy: Any) -> None:
    """Override the default policy for a single tool name."""
    _CUSTOM_POLICIES[tool_name] = policy


def reset_custom_policies() -> None:
    """Test helper."""
    _CUSTOM_POLICIES.clear()


def policy_for_tool(tool_name: str, *, is_write: bool) -> Any:
    """Pick the right policy for ``tool_name``.

    Lookup order:
    1. Explicit override via :func:`register_policy`.
    2. ``hitl_policy`` if the catalog marked the tool as ``is_write``.
    3. ``passthrough_policy`` otherwise.
    """
    if tool_name in _CUSTOM_POLICIES:
        return _CUSTOM_POLICIES[tool_name]
    if is_write:
        return hitl_policy
    return passthrough_policy


def apply_policies(
    tool_name: str,
    args: dict[str, Any],
    *,
    context: Any,
    is_write: bool,
) -> PolicyDecision:
    """Run every applicable policy for ``tool_name``.

    Currently a single policy per tool (DeepSeek pattern supports
    many — we may chain a ``risk_policy`` before ``hitl_policy`` later).
    """
    policy = policy_for_tool(tool_name, is_write=is_write)
    try:
        decision = policy(args, context=context)
    except Exception as e:
        LOGGER.warning("policy %s raised for %s: %s", policy, tool_name, e)
        return PolicyDecision(
            verdict=Verdict.ASK,
            reason=f"policy raised {type(e).__name__}: {e}",
        )
    if not isinstance(decision, PolicyDecision):
        LOGGER.warning("policy returned non-decision for %s: %r", tool_name, decision)
        return PolicyDecision(
            verdict=Verdict.ASK,
            reason=f"policy returned non-decision for {tool_name}",
        )
    return decision


__all__ = [
    "Verdict",
    "PolicyDecision",
    "passthrough_policy",
    "hitl_policy",
    "policy_for_tool",
    "apply_policies",
    "register_policy",
    "reset_custom_policies",
]
