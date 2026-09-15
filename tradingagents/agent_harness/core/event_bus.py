"""EventBus — unified event bus with 3 handler modes (W3-D3 E6).

dsh pattern: a single bus replaces scattered ``retry_async`` /
``circuit_breaker`` / ``audit.log`` glue. 3rd-party plugins
register handlers at ``Harness.events.subscribe(...)`` to hook into
cross-cutting concerns (audit, metrics, retry, notifications).

Three modes (roadmap §2.4 E6):

  emit       Fire-and-forget broadcast. All handlers run; payload
             is shared (not chained). Failures are logged and
             isolated so one bad handler can't break the rest.

  waterfall  Chain handlers in subscription order. Each handler
             receives the previous return value and returns the
             next payload. ``None`` from a handler means "no change".
             First short-circuit (``_StopPropagation``) wins.

  around     Middleware wrapper. Runs all ``<event>.pre`` waterfall
             handlers (each may transform kwargs), then the inner
             ``fn``, then ``<event>.post`` handlers (each may
             transform the result dict).

Why this exists
---------------
Today: tool calls are wrapped in ``PipelineContext`` (pre / post /
guard / execute / result), metrics are in ``Metrics``, audit in
``AuditLogger``, retry in ``RetryPolicy`` — each pluggable
separately. Cross-cutting plugins (e.g. "log every LLM call to Sentry")
have to monkey-patch all of them.

With EventBus a plugin does:
    harness.events.subscribe("tool.invoked", my_logger, mode="emit")
    harness.events.subscribe("llm.complete", transform_usage, mode="waterfall")
    harness.events.subscribe("tool.execute", my_middleware, mode="around")
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

# Type aliases for clarity
Handler = Callable[..., Any]
EmitHandler = Callable[[Any], None]
WaterfallHandler = Callable[[Any], Any]


class _StopPropagation(Exception):
    """Raised inside a waterfall handler to short-circuit the chain.

    The ``payload`` attribute is what the bus returns as the final
    waterfall result.
    """

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        super().__init__("stop propagation")


class EventBus:
    """In-process event bus with 3 handler modes.

    Thread-safety: not thread-safe (use one bus per worker). The
    bus is intended to be a per-``Harness`` singleton, which the
    ``Harness`` itself constructs single-threaded at startup.
    """

    VALID_MODES: tuple[str, ...] = ("emit", "waterfall", "around")

    def __init__(self) -> None:
        # event_type -> list of (mode, handler)
        self._handlers: dict[str, list[tuple[str, Handler]]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------
    def subscribe(
        self,
        event_type: str,
        handler: Handler | None = None,
        *,
        mode: str = "emit",
    ) -> Handler:
        """Register ``handler`` for ``event_type`` under ``mode``.

        Two forms:
          manual:  ``bus.subscribe("evt", my_fn, mode="emit")``
          deco:    ``@bus.subscribe("evt", mode="emit")``
                   ``def my_fn(payload): ...``

        Returns ``handler`` so both forms compose.
        """
        if handler is None:
            # Decorator form: ``subscribe("evt", mode=...)`` returns a
            # one-arg decorator. Mirrors ``SubagentProvider.register``.
            def _decorator(fn: Handler) -> Handler:
                self.subscribe(event_type, fn, mode=mode)
                return fn
            return _decorator
        if mode not in self.VALID_MODES:
            raise ValueError(
                f"unknown mode {mode!r}; valid: {self.VALID_MODES}"
            )
        if not callable(handler):
            raise TypeError(f"handler for {event_type!r} must be callable")
        self._handlers[event_type].append((mode, handler))
        return handler

    def unsubscribe(self, event_type: str, handler: Handler) -> bool:
        """Remove a previously-registered handler. Returns True if removed."""
        bucket = self._handlers.get(event_type, [])
        for i, (_, h) in enumerate(list(bucket)):
            if h is handler:
                bucket.pop(i)
                return True
        return False

    def handlers(self, event_type: str) -> list[tuple[str, Handler]]:
        """Return a copy of the (mode, handler) pairs for ``event_type``."""
        return list(self._handlers.get(event_type, []))

    def clear(self, event_type: str | None = None) -> None:
        """Remove all handlers for ``event_type`` (or every event)."""
        if event_type is None:
            self._handlers.clear()
        else:
            self._handlers.pop(event_type, None)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def emit(self, event_type: str, payload: Any = None) -> None:
        """Fire-and-forget broadcast to all ``emit`` handlers.

        Handlers may return anything; the return value is ignored.
        Exceptions are logged but never re-raised — a single bad
        handler must not poison the broadcast.
        """
        for mode, handler in list(self._handlers.get(event_type, ())):
            if mode != "emit":
                continue
            try:
                handler(payload)
            except Exception:
                LOGGER.exception(
                    "emit handler %s for %s raised",
                    getattr(handler, "__qualname__", repr(handler)),
                    event_type,
                )

    def waterfall(self, event_type: str, payload: Any = None) -> Any:
        """Chain ``waterfall`` handlers; each returns the next payload.

        A handler that returns ``None`` leaves the payload unchanged
        (use this for "I observed but didn't transform"). A handler
        that raises :class:`_StopPropagation` short-circuits the
        chain and ``payload`` becomes its ``.payload`` attribute.
        Other exceptions are logged and the chain continues with
        the last good payload.
        """
        current = payload
        for mode, handler in list(self._handlers.get(event_type, ())):
            if mode != "waterfall":
                continue
            try:
                result = handler(current)
            except _StopPropagation as stop:
                return stop.payload
            except Exception:
                LOGGER.exception(
                    "waterfall handler %s for %s raised",
                    getattr(handler, "__qualname__", repr(handler)),
                    event_type,
                )
                continue
            if result is not None:
                current = result
        return current

    def around(
        self, event_type: str, fn: Handler, *args: Any, **kwargs: Any,
    ) -> Any:
        """Middleware wrap.

        Runs ``waterfall("<event_type>.pre", kwargs)`` (each pre-handler
        may transform kwargs), then calls ``fn(*args, **merged_kwargs)``,
        then runs ``waterfall("<event_type>.post", {"result": ...})``
        (each post-handler may transform the result dict). Returns the
        final ``result`` from the post waterfall.
        """
        pre_kwargs = self.waterfall(event_type + ".pre", dict(kwargs))
        result = fn(*args, **pre_kwargs)
        post = self.waterfall(event_type + ".post", {"result": result})
        return post.get("result", result)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def __contains__(self, event_type: str) -> bool:
        return event_type in self._handlers

    def __len__(self) -> int:
        return sum(len(v) for v in self._handlers.values())

    def list_events(self) -> list[str]:
        return sorted(self._handlers)
