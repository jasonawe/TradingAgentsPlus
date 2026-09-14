"""MemoryManager — facade over L1/L2/L3 (v3 spec §6 init order step 6).

Process-wide singleton by default (Harness keeps one); call
:meth:`for_session` to switch between sessions.
"""
from __future__ import annotations

from typing import Any

from .base import MemoryEntry, MemoryLayer, MemoryScope
from .l1_session import SqliteSessionMemory
from .l2_preferences import UserPreferencesMemory
from .l3_references import AgentReferencesMemory


class MemoryManager:
    def __init__(
        self,
        data_dir: str | None = None,
        l1: SqliteSessionMemory | None = None,
        l2: UserPreferencesMemory | None = None,
        l3: AgentReferencesMemory | None = None,
    ) -> None:
        base = (data_dir or ".ta_cache")
        self.l1 = l1 or SqliteSessionMemory(db_path=f"{base}/session_memory.sqlite")
        self.l2 = l2 or UserPreferencesMemory(db_path=f"{base}/user_prefs.sqlite")
        self.l3 = l3 or AgentReferencesMemory(db_path=f"{base}/agent_refs.sqlite")

    # ------------------------------------------------------------------
    # Layer dispatch
    # ------------------------------------------------------------------
    def get_layer(self, scope: MemoryScope) -> MemoryLayer:
        return {
            MemoryScope.SESSION: self.l1,
            MemoryScope.PREFERENCES: self.l2,
            MemoryScope.REFERENCES: self.l3,
        }[scope]

    # ------------------------------------------------------------------
    # Convenience pass-throughs (default to L1 session scope)
    # ------------------------------------------------------------------
    def get(self, key: str, *, session_id: str | None = None, scope: MemoryScope | None = None) -> MemoryEntry | None:
        return self.get_layer(scope or MemoryScope.SESSION).get(key, session_id=session_id)

    def set(
        self,
        key: str,
        value: Any,
        *,
        session_id: str | None = None,
        ttl_seconds: int | None = None,
        scope: MemoryScope | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        return self.get_layer(scope or MemoryScope.SESSION).set(
            key, value, session_id=session_id, ttl_seconds=ttl_seconds, metadata=metadata,
        )

    def append_message(self, session_id: str, role: str, content: str) -> MemoryEntry:
        """Convenience: append chat message to L1 history."""
        if not isinstance(self.l1, SqliteSessionMemory):
            raise RuntimeError("l1 is not SqliteSessionMemory; cannot append_message")
        return self.l1.append_message(session_id, role, content)

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        if not isinstance(self.l1, SqliteSessionMemory):
            return []
        return self.l1.get_history(session_id)

    # ------------------------------------------------------------------
    # Cross-layer helpers
    # ------------------------------------------------------------------
    def remember_quote(self, symbol: str, quote: dict[str, Any]) -> MemoryEntry:
        """Cache a fetched quote at L3 (cross-session reference)."""
        return self.l3.set(f"quotes:{symbol}", quote, ttl_seconds=300)

    def recall_quote(self, symbol: str) -> MemoryEntry | None:
        return self.l3.get(f"quotes:{symbol}")
