"""Context Priority — 8-layer context assembly (v2 spec D4, N55 fix).

The 8 layers are project-defined (not an OpenBB standard). Lower
numerical priority wins; when two layers supply the same key, the
higher-priority layer overrides the lower one.

Token budgets (P4 design target, N57 fix — to be calibrated post-P4):

    Layer 1 (explicit):  200
    Layer 2 (skills):    200
    Layer 3 (tools):     800
    Layer 4 (files):     300
    Layer 5 (dashboard): 300
    Layer 6 (chat):     1500
    Layer 7 (global):    200
    Layer 8 (search):    500
    TOTAL:              4000

**Memory integration**(v3 spec §3 memory/, P8):
- Layer 6 (chat) auto-injects last N messages from L1 session memory
- Layer 7 (global) auto-injects user preferences from L2 memory
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from tradingagents.agent_harness.memory import MemoryManager


class Layer(IntEnum):
    EXPLICIT = 1
    SKILLS = 2
    TOOLS = 3
    FILES = 4
    DASHBOARD = 5
    CHAT = 6
    GLOBAL = 7
    SEARCH = 8


DEFAULT_TOKEN_BUDGETS: dict[Layer, int] = {
    Layer.EXPLICIT: 200,
    Layer.SKILLS: 200,
    Layer.TOOLS: 800,
    Layer.FILES: 300,
    Layer.DASHBOARD: 300,
    Layer.CHAT: 1500,
    Layer.GLOBAL: 200,
    Layer.SEARCH: 500,
}

TOTAL_BUDGET = 4000

DEFAULT_CHAT_HISTORY_LIMIT = 20


@dataclass
class ContextPriority:
    """Assembles the 8 layers, trims to budget, exposes ordered output.

    Parameters
    ----------
    budgets:
        Per-layer token caps.
    memory:
        Optional MemoryManager — when set, Layer 6 (chat) auto-injects
        session history and Layer 7 (global) auto-injects user prefs.
    chat_history_limit:
        Max messages to inject from L1 memory for the active session.
    """

    budgets: dict[Layer, int] = field(default_factory=lambda: dict(DEFAULT_TOKEN_BUDGETS))
    memory: "MemoryManager | None" = None
    chat_history_limit: int = DEFAULT_CHAT_HISTORY_LIMIT

    def assemble(
        self,
        layers: dict[Layer, dict[str, Any]] | None = None,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> list[tuple[Layer, dict[str, Any]]]:
        """Return layers ordered by priority (highest first), trimmed to budget.

        If ``memory`` is set, Layer 6/7 are auto-populated from L1/L2 memory
        when not explicitly provided.
        """
        layers = dict(layers or {})
        layers = self._inject_memory(layers, session_id=session_id, user_id=user_id)

        ordered = sorted(layers.items(), key=lambda kv: int(kv[0]))
        out: list[tuple[Layer, dict[str, Any]]] = []
        running = 0
        for layer, payload in ordered:
            cap = self.budgets.get(layer, 200)
            payload_tokens = self._estimate(payload)
            if running + payload_tokens > TOTAL_BUDGET:
                payload = self._trim(payload, max(0, TOTAL_BUDGET - running) * 4)
            out.append((layer, payload))
            running += self._estimate(payload)
        return out

    # ------------------------------------------------------------------
    # Memory injection (Layer 6 chat + Layer 7 global)
    # ------------------------------------------------------------------
    def _inject_memory(
        self,
        layers: dict[Layer, dict[str, Any]],
        *,
        session_id: str | None,
        user_id: str | None,
    ) -> dict[Layer, dict[str, Any]]:
        if self.memory is None:
            return layers

        # Layer 6 — chat history from L1 session memory.
        if session_id and Layer.CHAT not in layers:
            history = self.memory.get_history(session_id)
            history = history[-self.chat_history_limit:]
            if history:
                layers[Layer.CHAT] = {"messages": history}

        # Layer 7 — user preferences from L2 memory.
        if Layer.GLOBAL not in layers:
            uid = user_id or session_id or "default"
            prefs = self.memory.l2.list(session_id=uid)
            if prefs:
                layers[Layer.GLOBAL] = {p.key: p.value for p in prefs}

        return layers

    @staticmethod
    def _estimate(payload: Any) -> int:
        text = str(payload)
        return max(1, len(text) // 4)

    @staticmethod
    def _trim(payload: Any, target_chars: int) -> Any:
        if isinstance(payload, dict):
            text = str(payload)
            if len(text) <= target_chars:
                return payload
            return {"_trimmed": text[: max(0, target_chars)]}
        if isinstance(payload, str):
            return payload[: max(0, target_chars)]
        return payload
