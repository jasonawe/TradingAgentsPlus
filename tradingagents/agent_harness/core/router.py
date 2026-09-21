"""Task 7 — Router.

Spec §4.3:Router 负责 intent、op、symbol、slot 和 tier 分类,返回完整
``RouteDecision``。可以复用 ``tier.py`` 的 ``classify`` / ``classify_multi`` /
``extract_slots`` / ``extract_symbols`` / ``maybe_degrade_to_tier1`` —
本模块只组合这些纯函数,不复制 regex,不调用 tool / LLM。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .tier import (
    Intent,
    Op,
    Tier,
    classify,
    classify_multi,
    extract_slots,
    extract_symbols,
)
from ..runtime.models import RouteDecision

LOGGER = logging.getLogger(__name__)


# 把 (Intent, Op) 映射到 route_kind:
#   - 只读 CRUD / QUOTE / HISTORY / FUNDAMENTALS / NEWS / ALPHA
#     → DIRECT_READ
#   - 写 CRUD (CREATE / UPDATE / DELETE / BULK_DELETE / RUN)
#     → SYSTEM_COMMAND
#   - 分析 (ANALYSIS / COMPARE / UNKNOWN 且需要 LLM)
#     → AGENT_ANALYSIS

_READ_INTENTS = {
    Intent.QUOTE, Intent.HISTORY, Intent.FUNDAMENTALS,
    Intent.NEWS, Intent.ALPHA,
}

_WRITE_OPS = {Op.CREATE, Op.UPDATE, Op.DELETE, Op.BULK_DELETE, Op.RUN}

_ANALYSIS_INTENTS = {Intent.ANALYSIS, Intent.COMPARE}


@dataclass
class Router:
    """Pure routing layer — no tool / LLM imports."""

    def route(
        self,
        user_message: str,
        *,
        carry_symbols: list[str] | None = None,
        llm_unavailable: bool = False,
        prior_intent: Intent | None = None,
    ) -> RouteDecision:
        """Pure routing decision from text + optional context.

        Returns :class:`RouteDecision`. Does NOT perform any tool / LLM
        call — orchestrator / TurnCoordinator uses this to decide which
        downstream path (Tier 1 / SYSTEM_COMMAND / AGENT_ANALYSIS) to
        take.
        """
        msg = user_message or ""
        symbols = list(extract_symbols(msg))
        carry = list(carry_symbols or [])
        # primary (intent, op) — classify 永远返回 Op
        primary_intent, primary_op = classify(msg)
        slots = extract_slots(msg)

        # tier 决定
        tier_int, confidence, reason = self._decide_tier(
            primary_intent, symbols, msg, llm_unavailable,
        )

        # LLM 不可用时强制 Tier 1(只读 + 有 symbol)
        if llm_unavailable:
            if symbols or carry:
                tier_int = Tier.DIRECT
                confidence = max(confidence, 0.6)
                reason = "llm_unavailable → Tier 1 fallback"
            else:
                # 没 symbol 时只能 Tier 2 让上层 decide
                tier_int = Tier.PLAN_EXECUTE
                reason = "llm_unavailable but no symbol → Tier 2"
        # degrade hook for orchestrator (already handled above when llm_unavailable)

        # route_kind
        route_kind = self._route_kind(primary_intent, primary_op)

        # carry_symbols 填字段
        decision = RouteDecision(
            intent=primary_intent,
            op=primary_op,
            tier=int(tier_int),
            symbols=symbols,
            carry_symbols=carry,
            slots=slots,
            confidence=confidence,
            reason_code=reason,
            route_kind=route_kind,
        )
        return decision

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _decide_tier(
        self, intent: Intent, symbols: list[str], msg: str, llm_unavailable: bool,
    ) -> tuple[Tier, float, str]:
        """Compute tier / confidence / reason based on intent + symbols."""
        lower = (msg or "").lower()
        # Tier 3 触发:deep + 多 symbol
        if any(kw in lower for kw in ("深度对比", "全面分析", "comprehensive", "deep")) and len(symbols) >= 2:
            return Tier.WORKFLOW, 0.9, "deep + multi-symbol → Tier 3"
        # Tier 1:显式 read intent + 有 symbol
        if intent in _READ_INTENTS and symbols:
            return Tier.DIRECT, 0.85, f"{intent.value} + ticker → Tier 1"
        # Tier 1:CRUD 实体(LIST/READ,即使无 symbol) → Tier 1 走 SystemCommand
        if intent in (Intent.WATCHLIST, Intent.NOTE, Intent.ALERT, Intent.SCHEDULED,
                      Intent.RUN, Intent.REPORT) and symbols:
            return Tier.DIRECT, 0.85, f"{intent.value} + ticker → Tier 1"
        # Tier 2:分析 / 对比
        if intent in _ANALYSIS_INTENTS:
            return Tier.PLAN_EXECUTE, 0.8, "analysis → Tier 2"
        # 默认 Tier 2
        return Tier.PLAN_EXECUTE, 0.3, "no high-confidence keyword → Tier 2 fallback"

    def _route_kind(self, intent: Intent, op: Op | None) -> str:
        if op in _WRITE_OPS:
            return "SYSTEM_COMMAND"
        if intent in _ANALYSIS_INTENTS:
            return "AGENT_ANALYSIS"
        return "DIRECT_READ"


__all__ = ["Router"]
