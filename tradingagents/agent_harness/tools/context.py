"""ToolContext — context passed to every tool invocation (v3 spec §5.2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ToolContext:
    """Invocation context shared across tools in a single user turn."""

    session_id: str
    user_id: Optional[str] = None
    intent: Optional[str] = None
    tier: Optional[int] = None
    trace_id: Optional[str] = None
    extra: Optional[dict[str, Any]] = None

    def kw(self) -> dict[str, Any]:
        """Dataclass → dict for ``**kwargs`` spread."""
        return {k: v for k, v in self.__dict__.items() if v is not None}
