"""ToolRegistry — decorator + manual + entry_points registration (v3 spec §5.3).

Three registration modes:
- decorator: ``@tool_registry.register(...)`` over a function
- manual: ``registry.add(MyTool())``
- entry_points: ``registry.discover_entry_points()`` (group=agent_harness.tools)

Adding a new tool never requires changing Harness core code.
"""
from __future__ import annotations

import importlib.metadata as md
import logging
from typing import Callable, Optional

from .base import BaseTool, FunctionTool
from .permission import PermissionType
from .schema import RetryPolicy, ToolSchema

LOGGER = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._permission_index: dict[PermissionType, set[str]] = {
            p: set() for p in PermissionType
        }

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(
        self,
        *,
        name: str,
        description: str,
        args_schema: type,
        result_schema: type,
        permission: PermissionType | str = PermissionType.READ,
        timeout_seconds: float = 30.0,
        cache_ttl_seconds: int = 60,
        retry: Optional[RetryPolicy] = None,
        # §Step 23 — D2 metadata bag (capability tags, display_view
        # hint, lifecycle hook names, error normalization hints). Open
        # dict so individual callers can add keys without schema churn.
        metadata: dict | None = None,
    ) -> Callable[[Callable[..., object]], Callable[..., object]]:
        """Decorator that wraps the target function as a FunctionTool."""

        def decorator(func: Callable[..., object]) -> Callable[..., object]:
            schema = ToolSchema(
                name=name,
                description=description,
                args_schema=args_schema,
                result_schema=result_schema,
                permission=permission.value if isinstance(permission, PermissionType) else permission,
                timeout_seconds=timeout_seconds,
                cache_ttl_seconds=cache_ttl_seconds,
                retry=retry or RetryPolicy(),
                metadata=metadata or {},
            )
            self.add(FunctionTool(func, schema))
            return func

        return decorator

    def add(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool
        perm = tool.permission
        self._permission_index[perm].add(tool.name)
        LOGGER.info("tool registered: %s (permission=%s)", tool.name, perm.value)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"tool {name!r} not registered; known: {sorted(self._tools)}")
        return self._tools[name]

    def list_all(self) -> list[BaseTool]:
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    def list_by_permission(self, perm: PermissionType | str) -> list[BaseTool]:
        if isinstance(perm, str):
            perm = PermissionType(perm)
        return [self._tools[n] for n in self._permission_index[perm]]

    # ------------------------------------------------------------------
    # Entry points (N66 fix, 2026-09-11 — not auto-called by Harness)
    # ------------------------------------------------------------------
    def discover_entry_points(self, group: str = "agent_harness.tools") -> int:
        try:
            eps = md.entry_points(group=group)
        except Exception as e:
            LOGGER.warning("entry_points discovery failed: %s", e)
            return 0
        loaded = 0
        for ep in eps:
            try:
                tool = ep.load()()
                if isinstance(tool, BaseTool):
                    self.add(tool)
                    loaded += 1
                    LOGGER.info("discovered tool via entry_points: %s", tool.name)
            except Exception as e:
                LOGGER.warning("entry_points load failed for %s: %s", ep.name, e)
        return loaded


_default: Optional[ToolRegistry] = None


def get_default_tool_registry() -> ToolRegistry:
    global _default
    if _default is None:
        _default = ToolRegistry()
    return _default
