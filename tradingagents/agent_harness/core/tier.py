"""Tier routing (v2 spec D1, N44/N95 fix — extends fast_route, no separate classify_tier).

Routes a user query to one of three tiers:
- Tier 1: Direct Tool (server-side, no LLM)
- Tier 2: Plan + Execute (LLM plan → server execute → LLM synthesize)
- Tier 3: Full Workflow (multi-agent DAG)

Returns a :class:`RouteResult` with ``tier`` + ``intent`` + ``symbols`` so
the orchestrator can dispatch without duplicating routing logic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class Tier(int, Enum):
    DIRECT = 1
    PLAN_EXECUTE = 2
    WORKFLOW = 3


class Intent(str, Enum):
    QUOTE = "quote"
    HISTORY = "history"
    FUNDAMENTALS = "fundamentals"
    NEWS = "news"
    ALPHA = "alpha"
    WATCHLIST = "watchlist"
    SCHEDULED = "scheduled"
    COMPARE = "compare"
    ANALYSIS = "analysis"
    UNKNOWN = "unknown"


_TICKER_RE = re.compile(r"\b[A-Z0-9]{1,6}(?:\.[A-Z]{2})?\b")

_TIER1_KEYWORDS = {"价格", "多少钱", "报价", "quote", "价格?", "price", "rsi", "换手", "成交"}
_TIER2_KEYWORDS = {"估值", "分析", "对比", "compare", "估值合理性", "对比一下"}
_TIER3_KEYWORDS = {"深度", "综合", "详细", "全维度", "深度分析", "全面分析"}


@dataclass
class RouteResult:
    intent: Intent = Intent.UNKNOWN
    tier: Tier = Tier.PLAN_EXECUTE
    symbols: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "tier": int(self.tier),
            "symbols": list(self.symbols),
            "confidence": self.confidence,
            "reason": self.reason,
        }


def extract_symbols(message: str) -> list[str]:
    """Extract upper-case ticker-looking tokens (e.g. ``600036.SS``, ``AAPL``)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for tok in _TICKER_RE.findall(message or ""):
        if tok not in seen_set:
            seen_set.add(tok)
            seen.append(tok)
    return seen


def _hit(keywords: Iterable[str], message: str) -> bool:
    lower = (message or "").lower()
    return any(kw.lower() in lower for kw in keywords)


def classify_intent(message: str) -> Intent:
    """Map ``message`` to an :class:`Intent` enum (best-effort keyword)."""
    if _hit(_TIER1_KEYWORDS, message):
        return Intent.QUOTE
    if _hit({"新闻", "消息", "news"}, message):
        return Intent.NEWS
    if _hit({"rsi", "macd", "alpha", "因子"}, message):
        return Intent.ALPHA
    if _hit({"基本面", "pe", "pb", "roe", "财务"}, message):
        return Intent.FUNDAMENTALS
    if _hit({"关注", "自选", "watchlist"}, message):
        return Intent.WATCHLIST
    if _hit({"定时", "scheduled"}, message):
        return Intent.SCHEDULED
    if _hit(_TIER3_KEYWORDS, message):
        return Intent.ANALYSIS
    if _hit(_TIER2_KEYWORDS, message):
        return Intent.COMPARE
    return Intent.UNKNOWN


def fast_route(message: str) -> RouteResult:
    """Single-shot tier + intent classifier.

    N44 fix: ``fast_route`` returns tier directly; callers MUST NOT
    re-route via ``classify_tier`` (deprecated — keep single entry).
    """
    symbols = extract_symbols(message)
    intent = classify_intent(message)
    lower = (message or "").lower()

    # Tier 3 wins when "deep/comprehensive" + multi-symbol compare.
    if _hit(_TIER3_KEYWORDS, lower) and len(symbols) > 1:
        return RouteResult(
            intent=intent, tier=Tier.WORKFLOW, symbols=symbols,
            confidence=0.9, reason="deep + multi-symbol → Tier 3",
        )

    # Tier 1 short-circuit for explicit data queries.
    if intent in (Intent.QUOTE, Intent.HISTORY, Intent.FUNDAMENTALS, Intent.ALPHA,
                  Intent.WATCHLIST, Intent.SCHEDULED, Intent.NEWS) and symbols:
        return RouteResult(
            intent=intent, tier=Tier.DIRECT, symbols=symbols,
            confidence=0.85, reason=f"{intent.value} + ticker → Tier 1",
        )

    # Tier 2 default for analysis/compare intent.
    if intent in (Intent.COMPARE, Intent.ANALYSIS) and symbols:
        return RouteResult(
            intent=intent, tier=Tier.PLAN_EXECUTE, symbols=symbols,
            confidence=0.8, reason="analysis + ticker → Tier 2",
        )

    # Default: Tier 2 with low confidence; orchestrator will prompt user for ticker.
    return RouteResult(
        intent=intent, tier=Tier.PLAN_EXECUTE, symbols=symbols,
        confidence=0.3, reason="no high-confidence keyword match → Tier 2 fallback",
    )
