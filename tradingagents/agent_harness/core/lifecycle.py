"""Q6 / P2-9 — Lifecycle hook bridges (roadmap §11.4 P2-1).

A lightweight cross-cutting hook registry. The dsh harness exposes
``PreToolUse`` / ``PostToolUse`` / ``SessionStart`` as lifecycle
events that plugins can subscribe to; we mirror that contract so
plugins can attach telemetry / audit / safety wrappers without
reaching into orchestrator internals.

Hook points:
  - ``session_start`` — first event of ``stream_chat``
  - ``session_end``   — last event of ``stream_chat`` (success or error)
  - ``turn_start``    — every ``turn/started``
  - ``turn_end``      — every ``turn/ended``
  - ``step_start``    — every ``step/started``
  - ``step_end``      — every ``step/ended``
  - ``pre_tool_use``  — before each ``tool.invoke``
  - ``post_tool_use`` — after each ``tool.invoke`` (success or failure)

Design notes:
  - Hook callbacks receive a ``HookContext`` dataclass with the
    call-site's locals (``tool_name`` / ``args`` / ``result`` / etc.)
    so callers don't have to pack/unpack tuples.
  - ``fire(name, **kwargs)`` returns a list of ``HookResult`` so the
    caller can react to a "deny" decision returned by ``pre_tool_use``
    (e.g. cancel a destructive write).
  - Exceptions inside a hook are isolated: the failure is recorded
    in ``HookResult`` but does NOT prevent other hooks from running.
    This matches the dsh "around" middleware semantics.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

LOGGER = logging.getLogger(__name__)

HookName = Literal[
    "session_start", "session_end",
    "turn_start", "turn_end",
    "step_start", "step_end",
    "pre_tool_use", "post_tool_use",
]

ALL_HOOKS: tuple[HookName, ...] = (
    "session_start", "session_end",
    "turn_start", "turn_end",
    "step_start", "step_end",
    "pre_tool_use", "post_tool_use",
)


@dataclass
class HookContext:
    """Per-event payload passed to hook callbacks."""

    name: HookName
    session_id: str | None = None
    turn_id: int | None = None
    step_id: int | None = None
    step_name: str | None = None
    tool_name: str | None = None
    args: Any = None
    result: Any = None
    error: BaseException | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    """Outcome of a single hook callback invocation."""

    callback: Callable[..., Any]
    deny: bool = False
    modified: Any = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


HookCallback = Callable[[HookContext], Awaitable[Any] | Any]


class LifecycleHooks:
    """Process-wide registry for lifecycle hook callbacks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hooks: dict[str, list[HookCallback]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, name: HookName, callback: HookCallback) -> Callable[[], None]:
        """Register a hook; returns an idempotent deregister callable."""
        if name not in ALL_HOOKS:
            raise ValueError(f"unknown hook {name!r}; expected one of {ALL_HOOKS}")
        if not callable(callback):
            raise TypeError(f"hook callback for {name!r} must be callable")
        with self._lock:
            self._hooks[name].append(callback)

        def _deregister() -> None:
            with self._lock:
                try:
                    self._hooks[name].remove(callback)
                except ValueError:
                    pass

        return _deregister

    def unregister(self, name: HookName, callback: HookCallback) -> bool:
        with self._lock:
            try:
                self._hooks[name].remove(callback)
                return True
            except ValueError:
                return False

    def count(self, name: HookName | None = None) -> int:
        with self._lock:
            if name is None:
                return sum(len(v) for v in self._hooks.values())
            return len(self._hooks.get(name, []))

    def callbacks(self, name: HookName) -> list[HookCallback]:
        with self._lock:
            return list(self._hooks.get(name, []))

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------
    async def fire(self, name: HookName, context: HookContext) -> list[HookResult]:
        """Invoke all registered callbacks for ``name``.

        - Sync / async callbacks both supported.
        - Exceptions are isolated and recorded as ``HookResult.error``.
        - A callback returning ``{"deny": True, ...}`` (dict) or a
          ``HookResult`` with ``deny=True`` short-circuits the chain:
          remaining callbacks are skipped, but ``fire`` still returns
          the partial result list. Use ``any_deny(results)`` to
          detect.
        """
        results: list[HookResult] = []
        with self._lock:
            targets = list(self._hooks.get(name, []))
        for cb in targets:
            try:
                rv = cb(context)
                if asyncio.iscoroutine(rv):
                    rv = await rv
                # Callback may return a HookResult, a dict with
                # {"deny": bool, "modified": ...}, or None.
                deny = False
                modified: Any = None
                if isinstance(rv, HookResult):
                    deny = rv.deny
                    modified = rv.modified
                elif isinstance(rv, dict):
                    deny = bool(rv.get("deny", False))
                    modified = rv.get("modified", None)
                results.append(HookResult(callback=cb, deny=deny, modified=modified))
                if deny:
                    break
            except Exception as e:
                LOGGER.warning(
                    "hook %s callback failed: %s", name, e, exc_info=True,
                )
                results.append(HookResult(callback=cb, error=e))
        return results


def any_deny(results: list[HookResult]) -> bool:
    """True if any ``HookResult`` in ``results`` has ``deny=True``."""
    return any(r.deny for r in results)


__all__ = [
    "LifecycleHooks",
    "HookContext",
    "HookResult",
    "HookName",
    "ALL_HOOKS",
    "any_deny",
    "HookCallback",
]
