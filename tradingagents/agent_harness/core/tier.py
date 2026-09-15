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

# A-share prefix → exchange suffix (N121 fix, 2026-09-14).
# 6XXXXX → 上交所 .SS ; 0XXXXX / 3XXXXX → 深交所 .SZ ; 4XXXXX/5XXXXX
# 多数是基金/债券,这里保守不自动补,避免误判。
_A_SHARE_SS_PREFIXES = ("600", "601", "603", "605", "688")
_A_SHARE_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")


def normalize_symbol(token: str) -> str:
    """Auto-suffix 6-digit A-share codes so providers can route them.

    Examples
    --------
    >>> normalize_symbol("600036")
    '600036.SS'
    >>> normalize_symbol("000001")
    '000001.SZ'
    >>> normalize_symbol("600036.SS")
    '600036.SS'
    >>> normalize_symbol("AAPL")
    'AAPL'
    """
    s = (token or "").strip().upper()
    if not s or "." in s:
        return s
    if len(s) != 6 or not s.isdigit():
        return s
    if s.startswith(_A_SHARE_SS_PREFIXES):
        return s + ".SS"
    if s.startswith(_A_SHARE_SZ_PREFIXES):
        return s + ".SZ"
    return s

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
    """Extract upper-case ticker tokens + auto-suffix A-share codes (N121 fix).

    Examples
    --------
    >>> extract_symbols("分析 600036.SS 和 000001")
    ['600036.SS', '000001.SZ']
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for tok in _TICKER_RE.findall(message or ""):
        norm = normalize_symbol(tok)
        if norm not in seen_set:
            seen_set.add(norm)
            seen.append(norm)
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


def maybe_degrade_to_tier1(
    route: RouteResult,
    llm_factory: Any | None,
    circuit_breaker: Any | None,
) -> tuple[RouteResult, bool]:
    """§7.2 #1 — degrade Tier 2/3 to Tier 1 when LLM is unavailable.

    Returns ``(route, degraded)``:

    - ``degraded=True``  → caller MUST route via ``ShortCircuit`` (0 LLM).
      The returned route keeps the original ``symbols`` (so short_circuit
      has something to query), and forces ``intent=QUOTE`` (the most
      common Tier 1 fallback), ``tier=DIRECT``.
    - ``degraded=False`` → caller keeps the original route and proceeds
      with the Tier 2/3 StateGraph / workflow.

    Degrade conditions (any one triggers):

    1. ``llm_factory is None`` — no LLM wired at all.
    2. ``llm_factory`` exposes ``is_configured()`` returning False.
    3. ``circuit_breaker`` is provided and its state is ``OPEN``
       (provider tripped).  ``circuit_breaker.state`` is the public
       surface used elsewhere in the harness.

    Tier 1 short-circuit needs at least one ticker symbol — queries
    that have no ticker extracted are left alone (orchestrator
    handles them with the "ask user for ticker" prompt).
    """
    # Already Tier 1 — nothing to degrade.
    if route.tier == Tier.DIRECT:
        return route, False

    # No symbols → short_circuit cannot serve; keep original route so
    # the orchestrator can ask the user for a ticker.
    if not route.symbols:
        return route, False

    llm_ok = True
    if llm_factory is None:
        llm_ok = False
    elif hasattr(llm_factory, "is_configured"):
        try:
            llm_ok = bool(llm_factory.is_configured())
        except Exception:
            llm_ok = False
    if llm_ok and circuit_breaker is not None:
        # ``CircuitState.OPEN`` is the spec name; tolerate string match.
        state_name = getattr(circuit_breaker, "state", None)
        if state_name is not None and str(state_name).endswith("OPEN"):
            llm_ok = False

    if llm_ok:
        return route, False

    # Build degraded route.  Keep symbols, force intent=QUOTE so the
    # short-circuit knows which tool to invoke.
    reason = route.reason or ""
    if llm_factory is None:
        suffix = "no LLM wired → Tier 1 fallback"
    else:
        suffix = "LLM circuit open → Tier 1 fallback"
    return RouteResult(
        intent=Intent.QUOTE,
        tier=Tier.DIRECT,
        symbols=list(route.symbols),
        confidence=route.confidence,
        reason=f"{reason} | {suffix}" if reason else suffix,
    ), True
