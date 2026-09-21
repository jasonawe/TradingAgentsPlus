"""MemoryManager — facade over L1/L2/L3 (v3 spec §6 init order step 6).

Process-wide singleton by default (Harness keeps one); call
:meth:`for_session` to switch between sessions.
"""
from __future__ import annotations

from typing import Any

from .base import MemoryEntry, MemoryLayer, MemoryScope
from .l1_session import SqliteSessionMemory, UnsupportedProjectionBackend
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
    def get(
        self,
        key: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        scope: MemoryScope | None = None,
    ) -> MemoryEntry | None:
        # user_id is meaningful for L2 (preferences); L1 ignores it.
        # Only forward user_id when the target layer is L2 — otherwise
        # L1 raises TypeError on the unknown kwarg.
        layer = self.get_layer(scope or MemoryScope.SESSION)
        if scope is MemoryScope.PREFERENCES:
            return layer.get(key, session_id=session_id, user_id=user_id)
        return layer.get(key, session_id=session_id)

    def set(
        self,
        key: str,
        value: Any,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        ttl_seconds: int | None = None,
        scope: MemoryScope | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        layer = self.get_layer(scope or MemoryScope.SESSION)
        if scope is MemoryScope.PREFERENCES:
            return layer.set(
                key, value,
                session_id=session_id, user_id=user_id,
                ttl_seconds=ttl_seconds, metadata=metadata,
            )
        return layer.set(
            key, value,
            session_id=session_id,
            ttl_seconds=ttl_seconds, metadata=metadata,
        )

    def append_message(self, session_id: str, role: str, content: str) -> MemoryEntry:
        """Convenience: append chat message to L1 history."""
        if not isinstance(self.l1, SqliteSessionMemory):
            raise RuntimeError("l1 is not SqliteSessionMemory; cannot append_message")
        return self.l1.append_message(session_id, role, content)

    def append_projected_exchange(
        self, session_id: str, user_text: str, assistant_text: str,
        *, projection_key: str,
    ) -> Literal["applied", "already_applied"]:
        """Spec §22:Runtime projection 投影到 L1 history 的原子入口。

        Pass-through 到 ``SqliteSessionMemory.append_projected_exchange``。
        重复 ``projection_key`` 调用返回 ``already_applied`` 不追加 — 重启 /
        重试安全。
        """
        from typing import Literal
        if not isinstance(self.l1, SqliteSessionMemory):
            raise UnsupportedProjectionBackend(
                f"l1 is {type(self.l1).__name__}; cannot append_projected_exchange"
            )
        return self.l1.append_projected_exchange(
            session_id, user_text, assistant_text, projection_key=projection_key,
        )

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        if not isinstance(self.l1, SqliteSessionMemory):
            return []
        return self.l1.get_history(session_id)

    # ------------------------------------------------------------------
    # Per-session binding (cross-session isolation)
    # ------------------------------------------------------------------
    def for_session(self, session_id: str) -> "MemoryManager":
        """Return a session-bound view of this manager.

        All subsequent ``get / set / append_message / get_history`` calls
        on the returned view default to ``session_id``. The view shares
        the underlying L1/L2/L3 layers with the parent manager — no data
        duplication, no extra connection.

        Why this exists
        ---------------
        Most call sites inside ``Orchestrator`` / ``ContextPriority`` pass
        ``session_id=`` explicitly. But cross-session operations
        (e.g. cross-session reference resolution, per-session preload
        tools, multi-tab UIs that want to look at L1 for sid_A without
        mixing it up with sid_B) benefit from a *bound* view that hides
        the session_id plumbing.

        L2 preferences and L3 references are shared (user-level /
        agent-level state) — ``for_session`` does NOT scope them. If you
        need a session-scoped preference, write a separate L1 entry.

        Usage
        -----
        ::

            mm = harness.memory
            view = mm.for_session("s_abc123")
            view.append_message("user", "招商银行多少?")    # → sid=abc123
            msgs = view.get_history()                       # → sid=abc123

        Returns
        -------
        MemoryManager (new instance bound to the session). The original
        manager is unchanged.
        """
        return _SessionBoundManager(self, session_id)


class _SessionBoundManager(MemoryManager):
    """Session-bound view of a parent :class:`MemoryManager`.

    Implements the same interface as the parent but defaults ``session_id``
    for every operation that supports it. L1 layer operations are
    forwarded; L2 / L3 layer operations are unchanged (they're shared
    across sessions by design).

    We deliberately do NOT call ``MemoryManager.__init__`` — we reuse the
    parent's L1/L2/L3 layers verbatim, no new connections / DB files.
    """

    __slots__ = ("_parent", "_session_id")

    def __init__(self, parent: MemoryManager, session_id: str) -> None:
        self._parent = parent
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def l1(self):
        return self._parent.l1

    @property
    def l2(self):
        return self._parent.l2

    @property
    def l3(self):
        return self._parent.l3

    def get_layer(self, scope):
        return self._parent.get_layer(scope)

    # ---- Convenience pass-throughs with session_id default ----

    def get(self, key, *, session_id=None, scope=None):
        return self._parent.get(
            key,
            session_id=session_id if session_id is not None else self._session_id,
            scope=scope,
        )

    def set(self, key, value, *, session_id=None, ttl_seconds=None,
            scope=None, metadata=None):
        return self._parent.set(
            key,
            value,
            session_id=session_id if session_id is not None else self._session_id,
            ttl_seconds=ttl_seconds,
            scope=scope,
            metadata=metadata,
        )

    def append_message(self, session_id, role, content):
        """Bound: ``session_id=None`` (default) routes to bound sid."""
        effective = session_id or self._session_id
        return self._parent.append_message(effective, role, content)

    def get_history(self, session_id=None):
        return self._parent.get_history(
            session_id if session_id else self._session_id
        )

    def remember_quote(self, symbol, quote):
        # L3 cross-session — pass through unchanged.
        return self._parent.remember_quote(symbol, quote)

    def recall_quote(self, symbol):
        return self._parent.recall_quote(symbol)

    # Re-bind on a bound view returns a sibling (new bound view), not a
    # sub-view. Avoids accidental nested binding.
    def for_session(self, session_id: str) -> "_SessionBoundManager":
        return _SessionBoundManager(self._parent, session_id)

    def __repr__(self) -> str:
        return f"_SessionBoundManager(parent={self._parent!r}, session_id={self._session_id!r})"


    # ------------------------------------------------------------------
    # Cross-layer helpers
    # ------------------------------------------------------------------
    def remember_quote(self, symbol: str, quote: dict[str, Any]) -> MemoryEntry:
        """Cache a fetched quote at L3 (cross-session reference)."""
        return self.l3.set(f"quotes:{symbol}", quote, ttl_seconds=300)

    def recall_quote(self, symbol: str) -> MemoryEntry | None:
        return self.l3.get(f"quotes:{symbol}")
