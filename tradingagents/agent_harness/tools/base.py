"""BaseTool ABC + FunctionTool adapter (v3 spec §5.2)."""
from __future__ import annotations

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable

from tradingagents.agent_harness.core.timeout_enforcer import (
    CallTimeoutError, TimeoutEnforcer,
)
from tradingagents.agent_harness.data.cache import (
    ToolResultCache, get_default_cache,
)

from .context import ToolContext
from .permission import PermissionType
from .schema import RetryPolicy, ToolSchema

LOGGER = logging.getLogger(__name__)


class BaseTool(ABC):
    """Abstract base for every tool."""

    schema: ToolSchema

    @property
    def name(self) -> str:
        return self.schema.name

    @property
    def description(self) -> str:
        # Exposed so ToolSchemasLayerProvider (Layer.TOOLS context) can
        # surface a human-readable description alongside the tool name.
        # Without this, get_quote / add_to_watchlist / etc. all show
        # up as empty strings and the LLM has no idea what each tool does.
        return getattr(self.schema, "description", "") or ""

    @property
    def permission(self) -> PermissionType:
        return PermissionType(self.schema.permission)

    @abstractmethod
    async def invoke(self, args: Any, context: ToolContext) -> Any:
        """Execute the tool with a validated ``args`` instance + context."""


class FunctionTool(BaseTool):
    """Wrap a plain function (sync or async) as a BaseTool.

    The function signature is introspected; ``args`` must be a Pydantic
    instance matching the schema. The function's return value is expected
    to be a Pydantic instance of ``result_schema``.
    """

    def __init__(self, func: Callable[..., Any], schema: ToolSchema) -> None:
        self._func = func
        self.schema = schema
        self._is_coro = asyncio.iscoroutinefunction(func)

    @property
    def permission(self) -> PermissionType:
        return PermissionType(self.schema.permission)

    async def invoke(self, args: Any, context: ToolContext) -> Any:
        LOGGER.debug("tool invoke: %s args=%s", self.name, getattr(args, "model_dump", lambda: args)())

        # §7.3 #4 — Tool result cache (spec §9 P2 N62 fix).
        # Opt-in per invocation: caller must supply
        # ``ToolContext(tool_cache=...)`` AND the tool's
        # ``schema.cache_ttl_seconds`` must be > 0.  Falls back to None
        # otherwise — this keeps test isolation (no shared global state)
        # while letting the harness wire the cache explicitly for
        # production calls.  Key = (tool_name, stable_hash_of_args).
        cache_lookup: ToolResultCache | None = None
        if self.schema.cache_ttl_seconds > 0 and context.tool_cache is not None:
            cache_lookup = context.tool_cache
        cache_key: str | None = None
        if cache_lookup is not None:
            cache_key = ToolResultCache.make_key(self.name, args)
            hit = cache_lookup.get(cache_key)
            if hit is not None:
                LOGGER.debug("tool cache hit: %s key=%s", self.name, cache_key[:16])
                return hit

        sig = inspect.signature(self._func)
        kwargs: dict[str, Any] = {}
        if "context" in sig.parameters:
            kwargs["context"] = context
        if "args" in sig.parameters:
            kwargs["args"] = args

        # §Step 23 — record pre-invoke lifecycle event. Args are
        # sanitised via ``model_dump()`` when available so the
        # tracker never holds a reference to a live Pydantic model.
        # Hooks run synchronously; the harness already calls invoke()
        # under a lock so order is preserved.
        from .lifecycle import global_tracker as _tracker
        _tracker().pre(
            self.name,
            args=getattr(args, "model_dump", lambda: args)(),
        )

        # §7.2 #4 — wall-clock cap from ToolSchema.timeout_seconds.
        # 0 = no cap.  CallTimeoutError inherits asyncio.TimeoutError so
        # callers that catch ``asyncio.TimeoutError`` still match.
        timeout_seconds = self.schema.timeout_seconds
        enforcer = TimeoutEnforcer(
            op=f"tool.{self.name}", default_timeout_seconds=timeout_seconds,
        )
        try:
            if self._is_coro:
                # Build the coroutine so we can pass it to enforce().
                async def _coro():
                    return await self._func(**kwargs)
                result = await enforcer.enforce(_coro())
            else:
                # Sync path — runs in worker thread so the timeout still fires.
                result = await enforcer.enforce_sync(self._func, kwargs=kwargs)
        except BaseException as e:
            # §Step 23 — record error then re-raise. The exception
            # propagates unchanged so existing callers see the same
            # failure modes (TimeoutError, ProviderError, etc.).
            _tracker().error(
                self.name,
                args=getattr(args, "model_dump", lambda: args)(),
                error=e,
            )
            raise

        _tracker().post(
            self.name,
            args=getattr(args, "model_dump", lambda: args)(),
            result=result,
        )

        if cache_lookup is not None and cache_key is not None:
            cache_lookup.set(cache_key, result, ttl_seconds=self.schema.cache_ttl_seconds)
        return result
