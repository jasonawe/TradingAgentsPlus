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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


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


@dataclass
class ContextPriority:
    """Assembles the 8 layers, trims to budget, exposes ordered output."""

    budgets: dict[Layer, int] = field(default_factory=lambda: dict(DEFAULT_TOKEN_BUDGETS))

    def assemble(self, layers: dict[Layer, dict[str, Any]]) -> list[tuple[Layer, dict[str, Any]]]:
        """Return layers ordered by priority (highest first), trimmed to budget."""
        ordered = sorted(layers.items(), key=lambda kv: int(kv[0]))
        out: list[tuple[Layer, dict[str, Any]]] = []
        running = 0
        for layer, payload in ordered:
            cap = self.budgets.get(layer, 200)
            payload_tokens = self._estimate(payload)
            if running + payload_tokens > TOTAL_BUDGET:
                payload = self._trim(payload, cap - max(0, TOTAL_BUDGET - running))
            out.append((layer, payload))
            running += self._estimate(payload)
        return out

    @staticmethod
    def _estimate(payload: Any) -> int:
        # Crude heuristic: 4 chars ≈ 1 token.
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
