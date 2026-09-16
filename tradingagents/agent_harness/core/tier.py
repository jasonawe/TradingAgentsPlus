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
    # §P3-3 — CRUD entities (entity-level). The actual verb
    # (create / read / update / delete / list / run) is carried as a
    # separate :class:`Op` discriminator so one entity doesn't need 5
    # intent values per CRUD operation.
    WATCHLIST = "watchlist"
    NOTE = "note"
    ALERT = "alert"
    SCHEDULED = "scheduled"
    RUN = "run"
    REPORT = "report"
    COMPARE = "compare"
    ANALYSIS = "analysis"
    UNKNOWN = "unknown"


class Op(str, Enum):
    """Verb discriminator paired with :class:`Intent` (entity).

    One entity enum value + one op value fully describes a CRUD action
    (e.g. ``(Intent.NOTE, Op.CREATE)`` = "create a note"). The
    orchestrator's _CRUD_DISPATCH table maps every (entity, op) pair to
    a concrete tool invocation.

    §P3-3: not every entity supports every op (watchlist has no UPDATE;
    run has no CREATE-by-id, only RUN-by-ticker). The dispatch table
    documents the supported subset.
    """
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    LIST = "list"
    RUN = "run"  # for scheduled (run-now) and analysis (start new run)
    BULK_DELETE = "bulk_delete"  # delete-all-for-target: requires symbol, not id


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


# §P3-3 — entity × op keyword tables
_ENTITY_KW: dict[Intent, tuple[set[str], Op]] = {
    Intent.WATCHLIST: ({"关注", "自选", "watchlist"}, Op.LIST),
    Intent.NOTE:      ({"笔记", "备注", "memo", "note"}, Op.LIST),
    Intent.ALERT:     ({"告警", "提醒", "预警", "alert"}, Op.LIST),
    Intent.SCHEDULED: ({"定时", "cron", "定时任务", "scheduled"}, Op.LIST),
    Intent.RUN:       ({"分析任务", "运行", "跑一下", "analyse", "analyze", "analysis", "run"}, Op.LIST),
    Intent.REPORT:    ({"分析报告", "报告", "report"}, Op.LIST),
}

_OP_KW: dict[Op, set[str]] = {
    Op.CREATE: {"新建", "创建", "添加", "加入", "新增", "写", "建", "create", "add",
                "schedule", "安排", "新建一个", "建一个", "做一个",
                # '跑一下 / 启动 / 跑起来' for analysis-run start; the
                # dispatch table maps (RUN, CREATE) to
                # run_trading_agents_analysis so these belong here.
                "跑一下", "跑起来", "跑个", "启动", "run-it", "开始"},
    Op.LIST:   {"查看", "列出", "显示", "看看", "show", "list", "有哪些", "有什么",
                "全部的", "所有的", "列表"},
    Op.UPDATE: {"更新", "修改", "改", "调整", "edit", "update", "改一下"},
    Op.DELETE: {"删除", "移除", "去掉", "删", "delete", "remove", "取消关注", "停用",
                "关闭", "取消", "删掉"},
    Op.RUN:    {"立即触发", "立刻触发", "马上触发", "立即执行", "立刻执行",
                "立刻", "马上", "现在跑", "now-run", "trigger", "fire"},
}


def classify(message: str) -> tuple[Intent, Op]:
    """§P3-3 — entity × op classifier.

    Returns ``(intent, op)``. Order of detection:

    1. Entity keywords (``笔记`` / ``关注`` / ``定时`` etc.) — wins over
       the legacy read-only intent keywords (e.g. a message that says
       "列出我的笔记" → NOTE/LIST, not QUOTE).
    2. Op keywords inside the same message (``新建`` → CREATE,
       ``删除`` → DELETE, ``跑`` → RUN, ...). If no verb matches,
       falls back to the entity's default op (LIST for every CRUD
       entity).
    3. If no entity keyword matched, falls back to :func:`classify_intent`
       (legacy QUOTE / NEWS / ANALYSIS / ... classifier) and pairs it
       with ``Op.READ`` since the legacy intents are read-only.
    """
    text = (message or "").lower()

    # 1. Entity detection
    for intent, (kws, default_op) in _ENTITY_KW.items():
        if any(kw in text for kw in kws):
            # 2. Verb detection inside the entity match
            # §P3-3+: bulk-delete verbs (BULK_DELETE) override the
            # default verb so the dispatch table can pick the bulk
            # tool. Only meaningful for DELETE-family intents.
            if is_bulk_delete_intent(message):
                return intent, Op.BULK_DELETE
            for op, vkws in _OP_KW.items():
                if any(vk in text for vk in vkws):
                    return intent, op
            return intent, default_op

    # 3. Legacy fallback (read-only intents like QUOTE / NEWS / ANALYSIS)
    legacy = classify_intent(message)
    return legacy, Op.READ


def classify_multi(message: str) -> list[tuple[Intent, Op]]:
    """§P3-3 + — multi-intent classifier.

    Returns *all* (entity, op) pairs the user message matches, ordered
    by entity detection order (first hit wins for the primary intent).
    Used when a user expresses multiple CRUD actions in one turn, e.g.
    "看看这个资产的告警和笔记" -> [(NOTE, LIST), (ALERT, LIST)].

    Detection rules:

    1. For every :data:`_ENTITY_KW` entry, if the message contains any
       keyword, emit a (intent, op) pair. The op is the verb detected
       in the message (same rule as :func:`classify`); falls back to
       the entity's default op when no verb is present.
    2. If no entity matched, fall back to :func:`classify` and return
       a single-element list -- preserving the legacy read-only intent
       path (QUOTE / NEWS / ANALYSIS / ...).
    3. Deduplicate on (intent, op) so a message saying "笔记和笔记"
       does not produce duplicate tool calls.

    Note: for v1 we only treat *different* entities as multi-intent.
    Two reads against the same entity (e.g. "列出笔记 + 看一下笔记")
    collapse to a single (entity, op) pair.
    """
    text = (message or "").lower()
    pairs: list[tuple[Intent, Op]] = []
    seen: set[tuple[Intent, Op]] = set()
    for intent, (kws, default_op) in _ENTITY_KW.items():
        if not any(kw in text for kw in kws):
            continue
        op = default_op
        for vop, vkws in _OP_KW.items():
            if any(vk in text for vk in vkws):
                op = vop
                break
        key = (intent, op)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)

    if pairs:
        return pairs
    # Legacy single-intent fallback
    return [classify(message)]


_BULK_KEYWORDS: set[str] = {
    "都删", "都删除", "全删", "全部删除", "全部删", "清空", "清掉",
    "all", "delete-all", "delete_all", "purge", "wipe",
    "连同", "以及",
}

# Require a CRUD entity keyword alongside the bulk marker so a stray
# "全部" in an analysis question does not mis-fire. Conservative on
# purpose — false negatives fall through to single-record delete;
# false positives would route to bulk when the user wanted specific.
_BULK_INTENT_REQUIRED_ENTITIES: set[str] = {
    "告警", "笔记", "备注", "提醒", "预警",
    "alert", "note", "memo",
}


def is_bulk_delete_intent(message: str) -> bool:
    """§P3-3+ -- detect 'delete all for this asset' style requests.

    Returns True when the message contains both a bulk marker
    ('都删', '全部删除', 'all', 'purge', ...) AND a CRUD entity
    keyword ('告警', '笔记', 'alert', 'note', ...).
    """
    text = (message or "").lower()
    has_bulk = any(kw in text for kw in _BULK_KEYWORDS)
    has_entity = any(kw in text for kw in _BULK_INTENT_REQUIRED_ENTITIES)
    return has_bulk and has_entity


def classify_intent(message: str) -> Intent:
    """Map ``message`` to an :class:`Intent` enum (best-effort keyword).

    Kept for backward compatibility with callers that only want the
    entity-level intent. New code should use :func:`classify` which
    also returns the :class:`Op` discriminator.
    """
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


def fast_route_with_op(message: str) -> tuple[RouteResult, Op]:
    """Single-shot tier + entity/op. See :func:`fast_route` for the
    entity-only variant. Adds Op to the result tuple so callers that
    want CRUD verb awareness (orchestrator's _CRUD_DISPATCH) don't have
    to re-parse the user message.

    §P3-3+: when ``classify_multi`` detects 2+ CRUD pairs in the same
    message (e.g. "看看告警和笔记", "列出笔记和关注"), the single-tool
    Tier 1 short-circuit cannot serve them all — ShortCircuit only
    knows one tool per (intent, op) and silently drops the rest
    ("no Tier 1 tool for intent=NOTE" warning, no tool_call emitted).
    We force PLAN_EXECUTE in that case so the orchestrator's plan_node
    fans out via ``_multi_crud_plan``.
    """
    intent, op = classify(message)
    # §P3-3 — always pull symbols from the message so CRUD dispatch
    # args factories (e.g. _watchlist_crud_args / _alert_create_args /
    # _scheduled_create_args / _run_create_args) can populate their
    # ``symbol`` field from the user message. Even when route.tier is
    # DIRECT (CRUD reads/lists), the short_circuit may want to show
    # which symbol(s) the user mentioned.
    symbols = extract_symbols(message)
    multi_pairs = classify_multi(message)
    multi_intent = len({(p[0], p[1]) for p in multi_pairs}) >= 2
    # Tier 1 short-circuit for read-only data queries + CRUD reads/lists.
    # CRUD writes (CREATE/UPDATE/DELETE) need symbols/args from the
    # user_message and are left to the orchestrator's plan layer.
    # §P3-3+ — multi-intent CRUD queries skip Tier 1 entirely.
    if multi_intent:
        # Keep intent/op as the primary pair; tier bumps to PLAN_EXECUTE
        # so _multi_crud_plan in _plan() can fan out to every pair.
        return RouteResult(
            intent=intent, tier=Tier.PLAN_EXECUTE, symbols=symbols,
            confidence=0.85, reason=f"multi-intent ({len(multi_pairs)} pairs) -> Tier 2",
        ), op
    if intent in (Intent.WATCHLIST, Intent.NOTE, Intent.ALERT,
                  Intent.SCHEDULED, Intent.RUN, Intent.REPORT):
        # Tier 1 read paths can be served by the short-circuit; write
        # paths fall through to PLAN_EXECUTE where the dispatch table
        # decides which tool to invoke.
        if op in (Op.LIST, Op.READ):
            return RouteResult(
                intent=intent, tier=Tier.DIRECT, symbols=symbols,
                confidence=0.85, reason=f"{intent.value}+{op.value} → Tier 1",
            ), op
        return RouteResult(
            intent=intent, tier=Tier.PLAN_EXECUTE, symbols=symbols,
            confidence=0.8, reason=f"{intent.value}+{op.value} → Tier 2",
        ), op
    # Fall through to the legacy single-shot route for the read-only intents.
    return fast_route(message), op


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

    # §P3-3+ — never degrade CRUD intents. CRUD writes (delete / update
    # / create / bulk_delete / etc.) need the orchestrator's plan layer
    # to dispatch via _CRUD_DISPATCH; the Tier 1 short-circuit only
    # knows read-only data queries (get_quote / get_news / ...) and
    # would silently fall through to a wrong tool (``maybe_degrade_to_tier1``
    # sets intent=QUOTE which short-circuits to get_quote).
    if route.intent in {
        Intent.WATCHLIST, Intent.NOTE, Intent.ALERT,
        Intent.SCHEDULED, Intent.RUN, Intent.REPORT,
    }:
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
