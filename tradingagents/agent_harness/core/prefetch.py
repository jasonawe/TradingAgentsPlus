"""Prefetcher — §7.3 #9: 在 plan_node 完成后预拉 tool 结果。

背景:
  ``Orchestrator._plan`` 完成时 LLM 已经给出了要调的工具列表
  (quote / news / fundamentals …),但 ``_execute`` 还在等 LLM 写最终
  prompt / context 注入。这段空窗里我们可以**并行启动所有 plan
  step 的工具调用**,等 execute 真要拿结果时,大部分已经在 in-flight
  甚至已完成。

设计:
  - ``Prefetcher`` 不预测(LLM plan 已经做了);只是把 plan 翻译成
    一组 asyncio.Task,把 tool 调用并行触发。
  - 主线程的 ``_execute`` 先 ``await prefetcher.drain()`` — 等所有
    pre-fetched task 完成(或超时),命中 cache 直接返回。
  - 失败/超时的 task 不影响 execute 兜底重试 — pre-fetch 只是优化,
    不破坏已有错误处理路径。

缓存形态:
  - 简单 ``dict[tuple[str, frozenset], dict]`` — key 是
    ``(tool_name, frozenset(args.items()))``。
  - execute 阶段用同样的 key lookup;命中跳过 tool.invoke 直接拿结果。
  - 跨 session 不持久化(走 L3 跨 session cache 是另一个事)。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

LOGGER = logging.getLogger(__name__)


@dataclass
class PrefetchStats:
    """Aggregated observability counters for one :class:`Prefetcher` instance.

    Reset between sessions via :meth:`Prefetcher.reset_stats`.  All
    fields are cumulative since the last reset.
    """
    kick_off_calls: int = 0
    tasks_scheduled: int = 0
    tasks_deduped: int = 0
    tasks_skipped_unknown_tool: int = 0
    drain_calls: int = 0
    completed: int = 0
    failed: int = 0
    timed_out: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    predicted_calls: int = 0      # pattern-based predictions
    predicted_cache_hits: int = 0  # predictions that turned out correct

    def as_dict(self) -> dict:
        return {
            "kick_off_calls": self.kick_off_calls,
            "tasks_scheduled": self.tasks_scheduled,
            "tasks_deduped": self.tasks_deduped,
            "tasks_skipped_unknown_tool": self.tasks_skipped_unknown_tool,
            "drain_calls": self.drain_calls,
            "completed": self.completed,
            "failed": self.failed,
            "timed_out": self.timed_out,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "predicted_calls": self.predicted_calls,
            "predicted_cache_hits": self.predicted_cache_hits,
            "hit_rate": round(self.cache_hits / max(1, self.cache_hits + self.cache_misses), 3),
        }


def _make_cache_key(name: str, args: Any) -> tuple[str, frozenset]:
    """Stable cache key for ``(tool, args)``.``args`` may be dict or BaseModel."""
    if args is None:
        items: tuple = ()
    elif hasattr(args, "model_dump"):
        items = tuple(sorted(args.model_dump().items()))
    elif isinstance(args, dict):
        items = tuple(sorted(args.items()))
    else:
        items = (("value", str(args)),)
    return (name, frozenset(items))


@dataclass
class _PrefetchEntry:
    key: tuple[str, frozenset]
    task: asyncio.Task
    started_at: float
    finished_at: float | None = None
    result: Any = None
    error: BaseException | None = None


@dataclass
class PrefetchResult:
    """Snapshot returned by :meth:`Prefetcher.drain`."""

    entries: list[_PrefetchEntry] = field(default_factory=list)
    completed: int = 0
    failed: int = 0
    timed_out: int = 0

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def all_done(self) -> bool:
        return self.completed + self.failed + self.timed_out == self.total


class Prefetcher:
    """Fire tool calls in parallel after a plan is known, drain on execute.

    Usage::

        pf = Prefetcher()
        pf.kick_off(plan=state.plan, context=ctx, tool_registry=tools)
        # ... other work (LLM streaming / context injection) ...
        result = await pf.drain(timeout=10.0)
        for entry in result.entries:
            if entry.error is None:
                state.prefetched[entry.key] = entry.result

    Notes:
      - ``kick_off`` does NOT itself ``await`` the tasks; it schedules them
        on the running event loop and returns immediately.
      - The cache key intentionally ignores ``ToolContext`` (request-scoped
        user / surface hints) so that concurrent sessions with the same
        ``(tool, args)`` share results. If we ever need context-aware
        pre-fetch, bump the cache key to include ``ToolContext.session_id``.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = 8,
        predictor: "PlanPredictor | None" = None,
        history_capacity: int = 32,
    ) -> None:
        self._inflight: dict[tuple[str, frozenset], _PrefetchEntry] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._loop_id: int | None = None  # detect cross-loop misuse
        # §7.3 #9 polish: stats + pattern-based prediction
        self.stats = PrefetchStats()
        self._predictor = predictor
        self._history: list[tuple[str, ...]] = []
        self._history_capacity = max(1, history_capacity)

    def record_history(self, tools: Sequence[str]) -> None:
        """Record one completed plan's tool sequence (in call order).

        Used by :class:`PlanPredictor` to learn common follow-up tool
        calls.  Records are kept ring-buffer style (oldest evicted first).
        """
        seq = tuple(t for t in tools if t)
        if not seq:
            return
        self._history.append(seq)
        while len(self._history) > self._history_capacity:
            self._history.pop(0)

    def predict_next(self, prefix: Sequence[str]) -> tuple[str, ...]:
        """Predict the tools that follow ``prefix`` based on history.

        Returns the most common follow-up sequence (max 5 tools). Empty
        tuple when no predictor is wired or no history matches.
        """
        if self._predictor is None or not prefix:
            return ()
        return self._predictor.predict(self._history, tuple(prefix))

    def predict_and_kick_off(
        self,
        *,
        prefix: Sequence[str],
        context: Any,
        tool_registry: Any,
        default_args: dict[str, Any] | None = None,
    ) -> int:
        """Convenience: predict next tools and kick off a pre-fetch.

        ``default_args`` are forwarded to each predicted tool; tools that
        don't accept those args get silently skipped.  Returns count fired.
        """
        predictions = self.predict_next(prefix)
        if not predictions:
            return 0
        self.stats.predicted_calls += len(predictions)
        fired = 0
        plan: list[dict[str, Any]] = []
        for name in predictions:
            plan.append({"name": name, "args": dict(default_args or {})})
        return self.kick_off(plan=plan, context=context, tool_registry=tool_registry)

    def reset_stats(self) -> None:
        self.stats = PrefetchStats()

    def kick_off(
        self,
        *,
        plan: list[dict[str, Any]] | dict[str, Any],
        context: Any,
        tool_registry: Any,
    ) -> int:
        """Schedule a pre-fetch task for every plan step. Returns count fired.

        Accepts the two plan shapes produced by ``Orchestrator._plan``:
          - legacy list ``[{"action": "...", "args": {...}}, ...]``
          - PTC program ``{"mode": "ptc", "groups": [{"calls": [...]}, ...]}``
        """
        steps = self._flatten_plan(plan)
        if not steps:
            return 0
        loop = asyncio.get_event_loop()
        if self._loop_id is None:
            self._loop_id = id(loop)
        elif self._loop_id != id(loop):
            raise RuntimeError("Prefetcher reused across event loops; create a new instance")
        self.stats.kick_off_calls += 1
        fired = 0
        for step in steps:
            name = step.get("name") or step.get("action")
            args = step.get("args", {})
            if not name:
                continue
            key = _make_cache_key(name, args)
            if key in self._inflight:
                # already in-flight — dedupe
                self.stats.tasks_deduped += 1
                continue
            try:
                tool = tool_registry.get(name)
            except Exception as e:
                LOGGER.debug("prefetch skip %s: %s", name, e)
                self.stats.tasks_skipped_unknown_tool += 1
                continue
            task = asyncio.create_task(self._run(key, tool, args, context))
            import time as _time
            entry = _PrefetchEntry(key=key, task=task, started_at=_time.time())
            self._inflight[key] = entry
            self.stats.tasks_scheduled += 1
            fired += 1
        return fired

    async def _run(self, key, tool, args, context) -> None:
        async with self._semaphore:
            try:
                result = await tool.invoke(args, context)
                self._inflight[key].result = result
            except BaseException as e:  # noqa: BLE001 — record any failure
                self._inflight[key].error = e
            finally:
                import time as _time
                self._inflight[key].finished_at = _time.time()

    async def drain(self, *, timeout: float | None = None) -> PrefetchResult:
        """Await all in-flight tasks. Returns snapshot, never raises."""
        entries = list(self._inflight.values())
        if not entries:
            return PrefetchResult(entries=[])
        tasks = [e.task for e in entries]
        if timeout is not None:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
        else:
            await asyncio.gather(*tasks, return_exceptions=True)
            done, pending = set(tasks), set()
        snap = PrefetchResult(entries=list(entries))
        for e in entries:
            if e.task in pending:
                snap.timed_out += 1
                e.task.cancel()
            elif e.error is not None:
                snap.failed += 1
            else:
                snap.completed += 1
        # Push counts into cumulative stats
        self.stats.drain_calls += 1
        self.stats.completed += snap.completed
        self.stats.failed += snap.failed
        self.stats.timed_out += snap.timed_out
        return snap

    def lookup(self, name: str, args: Any) -> tuple[bool, Any]:
        """Non-blocking cache lookup; returns (hit, result)."""
        key = _make_cache_key(name, args)
        entry = self._inflight.get(key)
        if entry is None:
            self.stats.cache_misses += 1
            return False, None
        if entry.finished_at is None or entry.error is not None:
            self.stats.cache_misses += 1
            return False, None
        self.stats.cache_hits += 1
        return True, entry.result

    def reset(self) -> None:
        """Drop all in-flight entries (test/maintenance).

        Clears stats + history too so the next session starts fresh.
        """
        for e in self._inflight.values():
            if not e.task.done():
                e.task.cancel()
        self._inflight.clear()
        self._loop_id = None
        self.stats = PrefetchStats()
        self._history.clear()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _flatten_plan(plan) -> list[dict[str, Any]]:
        """Normalize the two plan shapes into ``[{"name": ..., "args": ...}]``."""
        if isinstance(plan, list):
            return plan
        if isinstance(plan, dict):
            mode = plan.get("mode")
            if mode == "ptc":
                groups = plan.get("groups") or []
                out: list[dict[str, Any]] = []
                for g in groups:
                    for call in g.get("calls") or []:
                        out.append({"name": call.get("name"), "args": call.get("args", {})})
                return out
            # generic {"steps": [...]} fallback
            steps = plan.get("steps") or []
            return [{"name": s.get("name") or s.get("action"), "args": s.get("args", {})} for s in steps]
        return []

    def __len__(self) -> int:
        return len(self._inflight)


# ---------------------------------------------------------------------------
# PlanPredictor — pattern-based prediction of next tool(s)
# ---------------------------------------------------------------------------
class PlanPredictor:
    """Predict the tools that follow a given prefix based on history.

    Strategy: find the longest matching prefix in history; collect every
    continuation (the rest of the sequence); return the most common one
    (ties broken by insertion order).  Pure-Python, no ML — just a
    n-gram-ish frequency table.

    ``max_lookahead`` caps the number of predicted tools returned (so a
    noisy history can't trigger 50 speculative fetches).
    """

    def __init__(self, *, max_lookahead: int = 5, min_support: int = 1) -> None:
        if max_lookahead < 1:
            raise ValueError("max_lookahead must be >= 1")
        self.max_lookahead = int(max_lookahead)
        if int(min_support) < 1:
            raise ValueError("min_support must be >= 1")
        self.min_support = int(min_support)

    def predict(
        self,
        history: Sequence[Sequence[str]],
        prefix: Sequence[str],
    ) -> tuple[str, ...]:
        """Return the most common continuation of ``prefix`` in ``history``.

        Empty tuple when nothing matches or ``prefix`` is empty.
        """
        if not prefix or not history:
            return ()
        prefix_t = tuple(prefix)
        # Frequency count of each continuation
        cont_counts: dict[tuple[str, ...], int] = {}
        first_seen: dict[tuple[str, ...], int] = {}
        for i, seq in enumerate(history):
            seq_t = tuple(seq)
            # find prefix match in seq
            for start in range(len(seq_t) - len(prefix_t) + 1):
                if seq_t[start:start + len(prefix_t)] == prefix_t:
                    cont = seq_t[start + len(prefix_t):start + len(prefix_t) + self.max_lookahead]
                    if not cont:
                        continue
                    cont_counts[cont] = cont_counts.get(cont, 0) + 1
                    first_seen.setdefault(cont, i)
        if not cont_counts:
            return ()
        # Filter by min_support, then pick highest count, then earliest
        candidates = [
            (cont, cnt, first_seen[cont])
            for cont, cnt in cont_counts.items()
            if cnt >= self.min_support
        ]
        if not candidates:
            return ()
        candidates.sort(key=lambda x: (-x[1], x[2]))
        return candidates[0][0]


__all__ = ["Prefetcher", "PrefetchResult", "PrefetchStats", "PlanPredictor", "_make_cache_key"]
