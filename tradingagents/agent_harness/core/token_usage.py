"""Per-session token usage accounting, partitioned by agent + surface.

Why this exists
---------------
LLM calls happen in 7+ places (orchestrator plan/synth, 6 sub-agents,
L3 judge). Each call returns ``usage`` but today it's discarded. To:

- Attribute cost to which agent burned tokens
- Surface per-agent budgets to the frontend
- Decide whether a multi-agent query is worth re-running with caching
- Drive P0-4 surface routing decisions

we need a single store per session that every LLM call feeds into.

Design
------
- ``TokenUsageStore`` is a per-session object (one per ``stream_chat``).
- A ``ContextVar`` carries the "active store" + "active agent name"
  through any nested ``await`` so any depth of sub-agent call ends up
  recording into the same session bucket.
- ``OpenAICompatibleProvider.complete()`` reads the contextvars and
  appends to the store when a real LLM response comes back.
- Sub-agents wrap their LLM call with ``track_agent(name)`` to set the
  agent label; the orchestrator uses ``"orchestrator"`` by default.

Surface partitioning (P0-4 hook-in)
-----------------------------------
Each record carries a ``surface`` tag (``"ui"`` / ``"audit"`` / ``"debug"``
/ ``"internal"``) so the P0-4 surface classification can later split
usage per destination without re-instrumenting.  Today every record
uses ``"ui"`` because we don't yet have separate surfaces.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

LOGGER = logging.getLogger(__name__)


# Default agent label when the caller didn't set one (orchestrator path).
DEFAULT_AGENT = "orchestrator"
# Default surface — P0-4 will introduce multiple surfaces.
DEFAULT_SURFACE = "ui"


_active_store: ContextVar["TokenUsageStore | None"] = ContextVar(
    "tradingagents_token_store", default=None,
)
_active_agent: ContextVar[str] = ContextVar(
    "tradingagents_current_agent", default=DEFAULT_AGENT,
)
_active_surface: ContextVar[str] = ContextVar(
    "tradingagents_current_surface", default=DEFAULT_SURFACE,
)


@dataclass
class UsageBucket:
    """Token totals for one (agent, surface) tuple.

    spec R5 (roadmap §3.3): disjoint 6 字段。
    input / output / cache_read / cache_write / reasoning / total
    + 调用计数 calls / 错误计数 errors。

    Disjoint = cache_read / cache_write 是 input 的子集(从 prompt 扣出来),
    reasoning 是 output 的子集(o-series 思考 token)。但账面上 6 个分开
    报数,让前端/审计能精确知道 cache 命中率、reasoning 占比。
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    errors: int = 0

    def add(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        total_tokens: int | None = None,
        errored: bool = False,
    ) -> None:
        """Accumulate disjoint fields.

        ``total_tokens`` 如果 provider 没给,默认从 6 个分量相加。
        """
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens
        self.reasoning_tokens += reasoning_tokens
        if total_tokens is None:
            total_tokens = (
                input_tokens + output_tokens
                + cache_read_tokens + cache_write_tokens
                + reasoning_tokens
            )
        self.total_tokens += total_tokens
        self.calls += 1
        if errored:
            self.errors += 1

    def to_dict(self) -> dict:
        """spec: 6 disjoint 字段 + 2 计数 字段(calls / errors) = 8 字段。"""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "errors": self.errors,
        }


class TokenUsageStore:
    """Per-session token accounting.

    Records every LLM call's input/output tokens keyed by ``(agent,
    surface)``.  Read-only views: ``summary()`` (full nested dict),
    ``totals()`` (single number), ``by_agent()`` / ``by_surface()``
    (rolled-up).
    """

    def __init__(self) -> None:
        # (agent, surface) -> UsageBucket
        self._buckets: dict[tuple[str, str], UsageBucket] = {}

    # ------------------------------------------------------------------
    # Recording — called by OpenAICompatibleProvider.complete()
    # ------------------------------------------------------------------
    def record(
        self,
        agent: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        total_tokens: int | None = None,
        surface: str = DEFAULT_SURFACE,
        errored: bool = False,
    ) -> None:
        key = (agent, surface)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = UsageBucket()
            self._buckets[key] = bucket
        bucket.add(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
            errored=errored,
        )

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------
    def by_agent(self) -> dict[str, dict]:
        """Aggregate by agent (all surfaces rolled up)."""
        agg: dict[str, dict] = {}
        for (agent, _surface), bucket in self._buckets.items():
            row = agg.setdefault(agent, {
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "reasoning_tokens": 0, "total_tokens": 0,
                "calls": 0, "errors": 0,
            })
            row["input_tokens"] += bucket.input_tokens
            row["output_tokens"] += bucket.output_tokens
            row["cache_read_tokens"] += bucket.cache_read_tokens
            row["cache_write_tokens"] += bucket.cache_write_tokens
            row["reasoning_tokens"] += bucket.reasoning_tokens
            row["total_tokens"] += bucket.total_tokens
            row["calls"] += bucket.calls
            row["errors"] += bucket.errors
        return agg

    def by_surface(self) -> dict[str, dict]:
        agg: dict[str, dict] = {}
        for (_agent, surface), bucket in self._buckets.items():
            row = agg.setdefault(surface, {
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "reasoning_tokens": 0, "total_tokens": 0,
                "calls": 0, "errors": 0,
            })
            row["input_tokens"] += bucket.input_tokens
            row["output_tokens"] += bucket.output_tokens
            row["cache_read_tokens"] += bucket.cache_read_tokens
            row["cache_write_tokens"] += bucket.cache_write_tokens
            row["reasoning_tokens"] += bucket.reasoning_tokens
            row["total_tokens"] += bucket.total_tokens
            row["calls"] += bucket.calls
            row["errors"] += bucket.errors
        return agg

    def totals(self) -> dict:
        """Single aggregate across all agents and surfaces."""
        input_t = sum(b.input_tokens for b in self._buckets.values())
        output_t = sum(b.output_tokens for b in self._buckets.values())
        cache_r = sum(b.cache_read_tokens for b in self._buckets.values())
        cache_w = sum(b.cache_write_tokens for b in self._buckets.values())
        reasoning = sum(b.reasoning_tokens for b in self._buckets.values())
        total_t = sum(b.total_tokens for b in self._buckets.values())
        calls = sum(b.calls for b in self._buckets.values())
        errors = sum(b.errors for b in self._buckets.values())
        return {
            "input_tokens": input_t,
            "output_tokens": output_t,
            "cache_read_tokens": cache_r,
            "cache_write_tokens": cache_w,
            "reasoning_tokens": reasoning,
            "total_tokens": total_t,
            "calls": calls,
            "errors": errors,
            "agents": len({a for (a, _s) in self._buckets}),
            "surfaces": len({s for (_a, s) in self._buckets}),
        }

    def summary(self) -> dict:
        """Nested shape suitable for SSE / API responses."""
        return {
            "totals": self.totals(),
            "by_agent": self.by_agent(),
            "by_surface": self.by_surface(),
        }

    def is_empty(self) -> bool:
        return not self._buckets


# --------------------------------------------------------------------------
# Context propagation — sub-agents wrap their LLM call with these helpers.
# --------------------------------------------------------------------------
@contextmanager
def track_agent(name: str) -> Iterator[None]:
    """Tag subsequent LLM calls with ``name`` as the agent.

    Usage::

        with track_agent("data_agent"):
            response = provider.complete_text(...)

    Nested ``track_agent`` calls form a stack — when the inner ``with``
    exits, the previous label is restored.  Safe to use inside
    ``asyncio.gather`` *if* each task runs in its own context (default).
    """
    token = _active_agent.set(name)
    try:
        yield
    finally:
        _active_agent.reset(token)


@contextmanager
def track_surface(name: str) -> Iterator[None]:
    """Tag subsequent LLM calls with ``name`` as the surface."""
    token = _active_surface.set(name)
    try:
        yield
    finally:
        _active_surface.reset(token)


@contextmanager
def attach_store(store: TokenUsageStore) -> Iterator[TokenUsageStore]:
    """Make ``store`` the active store for the current async task.

    Usage::

        store = TokenUsageStore()
        with attach_store(store):
            # any LLM call here records into store
            ...

    Nesting restores the previous store on exit.
    """
    token = _active_store.set(store)
    try:
        yield store
    finally:
        _active_store.reset(token)


def get_active_store() -> TokenUsageStore | None:
    return _active_store.get()


def get_active_agent() -> str:
    return _active_agent.get()


def get_active_surface() -> str:
    return _active_surface.get()
