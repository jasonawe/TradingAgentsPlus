"""Tool execution pipeline — 5-stage hookable pipeline (P0-3).

dsh's 5-stage tool pipeline::

  pre_execute (waterfall)        → allow / deny / ask
  monotonic guards (terminal)    → cannot be overridden downstream
  execute (around-dispatch)      → timeout / retry / metrics
  post_execute (waterfall)       → accept / block / replace / attach-context
  result (sync notification)     → frozen authoritative outcome (audit/metrics/UI)

We implement the minimum viable shape: hook registries per stage, defaults
that preserve current behaviour (no hooks = direct tool.invoke), and one
sample hook — :class:`DangerousToolGuard` — that flags write-cancel tools
for HITL approval.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

LOGGER = logging.getLogger(__name__)


class PipelineStage(str, Enum):
    PRE_EXECUTE = "pre_execute"
    GUARD = "guard"
    EXECUTE = "execute"
    POST_EXECUTE = "post_execute"
    RESULT = "result"


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"            # requires HITL approval
    REPLACE = "replace"    # post_execute only: result should be replaced


# --------------------------------------------------------------------------
# Pipeline context — mutable bag passed through every stage
# --------------------------------------------------------------------------
@dataclass
class PipelineContext:
    """One tool call's journey through the pipeline.

    Each stage may inspect / mutate fields. The pipeline emits a final
    ``PipelineResult`` based on ``error`` + ``result`` + ``decision``.
    """

    tool_name: str
    args: Any
    tool_context: Any  # ToolContext (avoid import cycle)

    # Populated by execute
    result: Any = None
    error: BaseException | None = None

    # Populated by pre_execute
    decision: Decision = Decision.ALLOW
    deny_reason: str | None = None
    ask_payload: dict[str, Any] = field(default_factory=dict)

    # Populated by post_execute when REPLACE chosen
    replaced_by: str | None = None  # name of the hook that replaced
    attached_context: dict[str, Any] = field(default_factory=dict)

    # Timestamps for metrics
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class PipelineResult:
    """Frozen outcome surfaced to caller + downstream (audit, L3 judge)."""

    tool_name: str
    ok: bool
    result: Any = None
    error: str | None = None
    denied: bool = False
    denied_by: str | None = None  # hook name
    needs_approval: bool = False
    approval_payload: dict[str, Any] | None = None
    replaced: bool = False
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "denied": self.denied,
            "needs_approval": self.needs_approval,
            "replaced": self.replaced,
            "elapsed_ms": self.elapsed_ms,
        }


# --------------------------------------------------------------------------
# Hook signatures
# --------------------------------------------------------------------------
PreExecuteHook = Callable[[PipelineContext], Awaitable[Decision]]
GuardHook = Callable[[PipelineContext], Awaitable[bool]]
PostExecuteHook = Callable[[PipelineContext], Awaitable[Decision | None]]
ResultHook = Callable[[PipelineContext, PipelineResult], None]  # sync


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------
class ToolPipeline:
    """Five-stage hookable execution pipeline.

    Usage::

        pipeline = ToolPipeline()
        pipeline.add_pre_execute(DangerousToolGuard.for_delete_tools())

        result = await pipeline.run(
            tool_name="delete_alert",
            args={"id": 42},
            tool_context=context,
            executor=tool.invoke,
        )
    """

    def __init__(self) -> None:
        self._pre: list[PreExecuteHook] = []
        self._guards: list[GuardHook] = []
        self._post: list[PostExecuteHook] = []
        self._result: list[ResultHook] = []

    # ------------------------------------------------------------------
    # Hook registration
    # ------------------------------------------------------------------
    def add_pre_execute(self, hook: PreExecuteHook) -> None:
        self._pre.append(hook)

    def add_guard(self, hook: GuardHook) -> None:
        self._guards.append(hook)

    def add_post_execute(self, hook: PostExecuteHook) -> None:
        self._post.append(hook)

    def add_result(self, hook: ResultHook) -> None:
        self._result.append(hook)

    def hook_count(self) -> dict[str, int]:
        return {
            "pre_execute": len(self._pre),
            "guard": len(self._guards),
            "post_execute": len(self._post),
            "result": len(self._result),
        }

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        tool_name: str,
        args: Any,
        tool_context: Any,
        executor: Callable[[Any, Any], Awaitable[Any]],
    ) -> PipelineResult:
        """Execute ``executor(args, tool_context)`` through 5 stages.

        Stages are skipped if no hooks are registered. Order:
        pre_execute → guard → execute → post_execute → result.
        """
        import time
        pctx = PipelineContext(
            tool_name=tool_name, args=args, tool_context=tool_context,
            started_at=time.monotonic(),
        )

        # 1. pre_execute (waterfall: first deny wins, ASK short-circuits)
        for hook in self._pre:
            try:
                pctx.decision = await hook(pctx)
            except Exception as e:
                LOGGER.warning("pre_execute hook failed: %s", e)
                pctx.decision = Decision.ALLOW  # fail-open
            if pctx.decision == Decision.DENY:
                return self._finalize(
                    pctx, denied=True, denied_by=getattr(hook, "__name__", "?"))
            if pctx.decision == Decision.ASK:
                return self._finalize(pctx, needs_approval=True)

        # 2. monotonic guards (terminal deny)
        for guard in self._guards:
            try:
                allowed = await guard(pctx)
            except Exception as e:
                LOGGER.warning("guard hook failed (fail-closed): %s", e)
                allowed = False  # guards fail-CLOSED for safety
            if not allowed:
                return self._finalize(
                    pctx, denied=True, denied_by=getattr(guard, "__name__", "?"))

        # 3. execute
        try:
            pctx.result = await executor(pctx.args, pctx.tool_context)
            pctx.error = None
        except BaseException as e:
            pctx.error = e

        # 4. post_execute (waterfall: any REPLACE wins)
        for hook in self._post:
            try:
                decision = await hook(pctx)
            except Exception as e:
                LOGGER.warning("post_execute hook failed: %s", e)
                continue
            if decision == Decision.REPLACE:
                pctx.replaced_by = getattr(hook, "__name__", "?")
            elif decision == Decision.DENY:
                return self._finalize(
                    pctx, denied=True, denied_by=getattr(hook, "__name__", "?"))

        return self._finalize(pctx)

    def _finalize(
        self,
        pctx: PipelineContext,
        *,
        denied: bool = False,
        denied_by: str | None = None,
        needs_approval: bool = False,
    ) -> PipelineResult:
        """Build the final PipelineResult, run result hooks, and return.

        All terminal outcomes (allow / deny / needs_approval / error /
        replaced) flow through here so observers always see the outcome.
        """
        import time
        pctx.finished_at = time.monotonic()
        result = self._result_for(
            pctx, denied=denied, denied_by=denied_by,
            needs_approval=needs_approval,
        )
        # 5. result observation (synchronous notifications — metrics / log / UI)
        for hook in self._result:
            try:
                hook(pctx, result)
            except Exception as e:
                LOGGER.warning("result hook failed: %s", e)
        return result

    def _result_for(
        self,
        pctx: PipelineContext,
        *,
        denied: bool = False,
        denied_by: str | None = None,
        needs_approval: bool = False,
    ) -> PipelineResult:
        elapsed_ms = (pctx.finished_at - pctx.started_at) * 1000 if pctx.finished_at else 0.0
        if denied:
            return PipelineResult(
                tool_name=pctx.tool_name, ok=False,
                error=pctx.deny_reason or "denied",
                denied=True, denied_by=denied_by,
                elapsed_ms=elapsed_ms,
            )
        if needs_approval:
            return PipelineResult(
                tool_name=pctx.tool_name, ok=False,
                error="approval_required",
                needs_approval=True,
                approval_payload=pctx.ask_payload,
                elapsed_ms=elapsed_ms,
            )
        if pctx.error is not None:
            return PipelineResult(
                tool_name=pctx.tool_name, ok=False,
                error=str(pctx.error),
                elapsed_ms=elapsed_ms,
            )
        return PipelineResult(
            tool_name=pctx.tool_name, ok=True,
            result=pctx.result,
            replaced=bool(pctx.replaced_by),
            elapsed_ms=elapsed_ms,
        )


# --------------------------------------------------------------------------
# Sample pre_execute hook — DangerousToolGuard
# --------------------------------------------------------------------------
class DangerousToolGuard:
    """Pre_execute hook that flags destructive tools as needing HITL approval.

    Default tool name patterns: ``delete_*``, ``cancel_*``, ``drop_*``,
    ``clear_*``, ``reset_*``. Custom patterns may be passed.

    The hook returns :attr:`Decision.ASK` for matching tools (the
    orchestrator surfaces this as a pending_approval result entry),
    :attr:`Decision.ALLOW` otherwise.
    """

    DEFAULT_PATTERNS = ("delete_", "cancel_", "drop_", "clear_", "reset_")

    def __init__(self, extra_patterns: tuple[str, ...] = ()) -> None:
        self._patterns = self.DEFAULT_PATTERNS + tuple(extra_patterns)

    @classmethod
    def for_delete_tools(cls) -> "DangerousToolGuard":
        return cls()

    def __call__(self, pctx: PipelineContext) -> Awaitable[Decision]:
        name = pctx.tool_name
        async def _decide() -> Decision:
            if any(name.startswith(p) for p in self._patterns):
                pctx.ask_payload = {
                    "tool": name,
                    "args": pctx.args,
                    "reason": "destructive_tool_requires_approval",
                }
                return Decision.ASK
            return Decision.ALLOW
        return _decide()
