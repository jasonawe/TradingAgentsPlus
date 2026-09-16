"""Memory layer — v3 spec §3 memory/.

Three layers:
- L1 session:    SqliteSaver — per-session chat history (short-term, TTL ≤ 24h)
- L2 preferences: user-level settings (long-term, e.g. preferred ticker format)
- L3 references: agent-level knowledge base (cross-session, e.g. cached quotes)

MemoryManager exposes all three layers as a single facade; the harness
keeps one instance per session (or process-wide) and queries layers by
scope.
"""
from .base import MemoryEntry, MemoryLayer, MemoryScope
from .l1_session import SqliteSessionMemory
from .event_log import Event, EventLog, SurfaceType, TYPE_TO_SURFACE, classify_surface
from .l2_preferences import UserPreferencesMemory
from .l3_references import AgentReferencesMemory
from .manager import MemoryManager, _SessionBoundManager

__all__ = [
    "MemoryEntry",
    "MemoryLayer",
    "MemoryScope",
    "SqliteSessionMemory",
    "UserPreferencesMemory",
    "AgentReferencesMemory",
    "MemoryManager",
    "_SessionBoundManager",
]
