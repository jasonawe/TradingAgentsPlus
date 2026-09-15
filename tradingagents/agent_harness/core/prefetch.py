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
from typing import Any, Awaitable, Callable

LOGGER = logging.getLogger(__name__)


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

    def __init__(self, *, max_concurrent: int = 8) -> None:
        self._inflight: dict[tuple[str, frozenset], _PrefetchEntry] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._loop_id: int | None = None  # detect cross-loop misuse

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
        fired = 0
        for step in steps:
            name = step.get("name") or step.get("action")
            args = step.get("args", {})
            if not name:
                continue
            key = _make_cache_key(name, args)
            if key in self._inflight:
                # already in-flight — dedupe
                continue
            try:
                tool = tool_registry.get(name)
            except Exception as e:
                LOGGER.debug("prefetch skip %s: %s", name, e)
                continue
            task = asyncio.create_task(self._run(key, tool, args, context))
            import time as _time
            entry = _PrefetchEntry(key=key, task=task, started_at=_time.time())
            self._inflight[key] = entry
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
        return snap

    def lookup(self, name: str, args: Any) -> tuple[bool, Any]:
        """Non-blocking cache lookup; returns (hit, result)."""
        key = _make_cache_key(name, args)
        entry = self._inflight.get(key)
        if entry is None:
            return False, None
        if entry.finished_at is None or entry.error is not None:
            return False, None
        return True, entry.result

    def reset(self) -> None:
        """Drop all in-flight entries (test/maintenance)."""
        for e in self._inflight.values():
            if not e.task.done():
                e.task.cancel()
        self._inflight.clear()
        self._loop_id = None

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


__all__ = ["Prefetcher", "PrefetchResult", "_make_cache_key"]
