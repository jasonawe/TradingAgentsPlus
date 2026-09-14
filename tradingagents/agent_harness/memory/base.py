"""MemoryLayer ABC + MemoryEntry + MemoryScope (v3 spec §3 memory/base.py)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MemoryScope(str, Enum):
    """Which layer owns the memory (spec §3 memory/)."""
    SESSION = "l1_session"
    PREFERENCES = "l2_preferences"
    REFERENCES = "l3_references"


class MemoryEntry(BaseModel):
    """One memory record."""

    key: str
    value: Any
    scope: MemoryScope
    session_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryLayer(ABC):
    """Abstract base for every memory layer."""

    scope: MemoryScope

    @abstractmethod
    def get(self, key: str, *, session_id: str | None = None) -> MemoryEntry | None:
        ...

    @abstractmethod
    def set(
        self,
        key: str,
        value: Any,
        *,
        session_id: str | None = None,
        ttl_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        ...

    @abstractmethod
    def delete(self, key: str, *, session_id: str | None = None) -> bool:
        ...

    @abstractmethod
    def list(self, *, session_id: str | None = None, prefix: str | None = None) -> list[MemoryEntry]:
        ...
