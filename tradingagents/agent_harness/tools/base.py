"""BaseTool ABC + FunctionTool adapter (v3 spec §5.2)."""
from __future__ import annotations

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable

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
        sig = inspect.signature(self._func)
        kwargs: dict[str, Any] = {}
        if "context" in sig.parameters:
            kwargs["context"] = context
        if "args" in sig.parameters:
            kwargs["args"] = args
        if self._is_coro:
            return await self._func(**kwargs)
        return await asyncio.to_thread(self._func, **kwargs)
