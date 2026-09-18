"""Tier routing (v2 spec D1, N44/N95 fix — extends fast_route, no separate classify_tier).

Routes a user query to one of three tiers:
- Tier 1: Direct Tool (server-side, no LLM)
- Tier 2: Plan + Execute (LLM plan → server execute → LLM synthesize)
- Tier 3: Full Workflow (multi-agent DAG)

Returns a :class:`RouteResult` with ``tier`` + ``intent`` + ``symbols`` so
the orchestrator can dispatch without duplicating routing logic.
"""
from __future__ import annotations

import logging
LOGGER = logging.getLogger(__name__)

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


_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{4,}|\d{5,6})(?:\.[A-Z]{2})?(?![A-Za-z0-9])")  # P0: char-class lookbehind (handles CJK boundaries); see sysissues.md #1  # P0: drop bare ".SS"/".SZ" matches (see sysissues.md #1)

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
    if not s:
        return s
    if "." in s:
        # Validate the digits portion so we don't silently accept
        # malformed tickers like '00031.SS' (5 digits).
        digits = s.split(".")[0]
        if digits.isdigit() and len(digits) != 6:
            LOGGER.warning(
                "normalize_symbol: invalid A-share length %d for %r (expected 6)",
                len(digits), s,
            )
        return s
    if len(s) != 6 or not s.isdigit():
        # 5-digit numeric tokens like '00031' almost certainly mean a
        # typo'd 6-digit A-share code (user dropped a digit). Log a
        # warning and return original so downstream can surface it.
        if s.isdigit() and len(s) == 5:
            LOGGER.warning(
                "normalize_symbol: 5-digit ticker %r looks like a typo (A-shares are 6 digits)",
                s,
            )
        return s
    if s.startswith(_A_SHARE_SS_PREFIXES):
        return s + ".SS"
    if s.startswith(_A_SHARE_SZ_PREFIXES):
        return s + ".SZ"
    return s


def is_valid_symbol(s: str) -> bool:
    """Best-effort validity check for a carry-forward / persisted ticker.

    Rules
    -----
    - 6-digit A-share (with optional .SS / .SZ) -> valid
    - 4+ letter latin ticker (e.g. AAPL, TSLA, NVDA) -> valid
    - anything else (including bare "SS" / "SZ" / "ETF" leftovers from
      regex extraction, 2–3 char tokens, etc.) -> invalid
    """
    if not s:
        return False
    t = s.strip().upper()
    if not t:
        return False
    # Strip suffix for length check
    base = t.split(".")[0] if "." in t else t
    if len(base) >= 4 and base.isalpha():
        return True
    if base.isdigit() and len(base) == 6:
        return True
    return False


def sanitize_symbols(symbols: list[str] | None) -> list[str]:
    """Drop invalid tickers from a list before persistence / dispatch."""
    if not symbols:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        norm = normalize_symbol(s)
        if is_valid_symbol(norm) and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §Step1 — Hybrid slot filling foundation.
#
# extract_slots() pulls structured parameters (time_range / threshold /
# direction / cron / limit) out of a user message before route + dispatch
# decide which tier to serve. Each slot is independent: missing slots
# remain None and the orchestrator falls back to LLM synthesis to fill
# them in (Stage 2 of the hybrid plan). The dict is intentionally
# loose-typed — the orchestrator validates per-tool args downstream.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import datetime as _dt
from typing import Any

_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
_TIME_UNIT_CN = {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400, "日": 86400, "周": 604800}
_DIRECTION_CN = {"超过": "above", "高于": "above", "大于": "above", "向上": "above",
                 "涨破": "above", "涨过": "above", "above": "above",
                 "低于": "below", "小于": "below", "向下": "below",
                 "跌破": "below", "跌穿": "below", "below": "below"}


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def extract_slots(message: str) -> dict[str, Any]:
    """Pull structured parameter slots out of a user message.

    Returns a dict with any subset of these keys (None when not found):

    - ``time_range``: ``(since_ts, until_ts)`` ISO strings for windows like
      "近 30 天" / "过去 5 小时" / "last 7 days".
    - ``threshold``: numeric value from phrases like "超过 50" / "高于 5.2".
    - ``direction``: "above" or "below" paired with threshold.
    - ``limit``: int from "最近 N 条" / "前 N 个" / "limit N".
    - ``cron``: cron string from "每天早上 9 点" / "每个交易日收盘" (basic
      natural-language cron — only the common cases; complex patterns
      still go to LLM).

    The function is best-effort: it never raises and never modifies the
    caller-visible message. Use the helper for cheap pre-routing hints;
    rely on the LLM synthesizer for anything ambiguous.
    """
    out: dict[str, Any] = {}
    if not message:
        return out
    lower = message.lower()

    # ── time_range ────────────────────────────────────────────────
    # Patterns: 近 N 天 / 过去 N 小时 / last N days / 过去 N 分钟
    m = re.search(r"(?:近|过去|最近)\s*(\d+)\s*(秒|分钟|小时|天|日|周)", lower)
    if not m:
        m = re.search(r"last\s+(\d+)\s+(seconds?|minutes?|hours?|days?|weeks?)", lower)
        if m:
            n = int(m.group(1))
            unit = m.group(2).lower()
            seconds = {"second": 1, "seconds": 1, "minute": 60, "minutes": 60,
                       "hour": 3600, "hours": 3600, "day": 86400, "days": 86400,
                       "week": 604800, "weeks": 604800}.get(unit, 86400)
            until = _now_utc()
            since = until - _dt.timedelta(seconds=n * seconds)
            out["time_range"] = (since.isoformat(), until.isoformat())
    if "time_range" not in out and m:
        n = int(m.group(1))
        unit = m.group(2)
        seconds = _TIME_UNIT_CN.get(unit, 86400)
        until = _now_utc()
        since = until - _dt.timedelta(seconds=n * seconds)
        out["time_range"] = (since.isoformat(), until.isoformat())

    # §Step5.B — explicit date ranges: "2026-09-01 之后" / "after 2026-09-01"
    # / "2026-09-01 ~ 2026-09-10". Sets only since_ts/until_ts on the
    # time_range tuple — leaves any pre-existing rolling window alone.
    if "time_range" not in out:
        iso = re.search(r"(\d{4}-\d{1,2}-\d{1,2})", message)
        if iso:
            try:
                d = _dt.date.fromisoformat(iso.group(1))
                # Bind on directional cue words if present, else use
                # the bare date as lower bound.
                after_kw = any(k in message for k in ("之后", "以后", "after", "since"))
                before_kw = any(k in message for k in ("之前", "以前", "before", "until"))
                since_iso = _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc).isoformat()
                if after_kw and not before_kw:
                    out["time_range"] = (since_iso, _now_utc().isoformat())
                elif before_kw and not after_kw:
                    out["time_range"] = (_dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc).isoformat(), since_iso)
            except ValueError:
                pass

    # ── scope (Step 10) ───────────────────────────────────────
    # Patterns: '我的笔记' / 'my notes' / '所有的笔记' / '我的全部笔记' → user scope.
    # Read paths already default to user-scope; this slot is the UI hint
    # that the user explicitly asked for *their* data — used by the
    # result_summary event so the badge says '我的笔记' instead
    # of just '笔记'. Implicit (no scope keyword) keeps working.
    if any(kw in message for kw in ("我的", "my ", "i have", "mine")):
        out["scope"] = "user"
    elif any(kw in message for kw in ("公开", "全部", "所有的", "all ", "public")):
        out["scope"] = "all"

    # ── threshold + direction ─────────────────────────────────────
    # Patterns: 超过 50 / 高于 5.2 / 低于 30 / above 100 / below 200
    m = re.search(r"(超过|高于|大于|向上|涨破|涨过|低于|小于|向下|跌破|跌穿|above|below)\s*([\d.]+)", lower)
    if m:
        kw = m.group(1)
        val = float(m.group(2))
        # §Step2 — use the _DIRECTION_CN table (single source of truth)
        # instead of a hardcoded whitelist, so any new direction word
        # added to the table (涨破/涨过/向上/...) routes properly.
        direction = _DIRECTION_CN.get(kw, "above")
        out["threshold"] = val
        out["direction"] = direction

    # ── limit ─────────────────────────────────────────────────────
    # §Step5.A — multi-pattern union covering Chinese / English /
    # adjective variants. Tried in priority order; first hit wins.
    # Capped at 200 to bound the call (read tools shouldn't page out
    # the entire DB by accident).
    limit_m = (
        # "前 X 条 / limit X / 最近 X 条" with explicit count word
        re.search(r"(?:最近|前|limit)\s*(\d+)\s*(?:条|个|只|条记录|条笔记)?", lower)
        # "X 条 / X 个" standalone — "5 条笔记"
        or re.search(r"(\d+)\s*(?:条|个|只|条记录|条笔记)", lower)
        # "只看 X / 只要 X / 仅 X" — restrictive prefix
        or re.search(r"(?:只|仅|就要)\s*(\d+)\s*(?:条|个|只)?", lower)
        # "top X / 前 X 名" English-leaning fallback
        or re.search(r"\btop\s*(\d+)", lower)
    )
    if limit_m:
        n = int(limit_m.group(1))
        if 0 < n <= 200:
            out["limit"] = n

    # ── cron (basic NL → cron) ────────────────────────────────────
    # Patterns:
    #   每天早上 N 点         -> "0 N * * *"
    #   每个交易日收盘         -> "0 15 * * 1-5"  (15:00 CST daily close)
    #   每天 / 每日 / daily   -> "0 9 * * *"    (default 09:00)
    #   weekly / 每周 N        -> "5 N * * 1"    (default Monday 09:00)
    if any(kw in lower for kw in ("每天", "每日", "天天", "daily")):
        m = re.search(r"(?:每天|每日|天天)?(?:早上|上午|早上|早晨)?\s*(\d{1,2})\s*点", lower)
        hour = int(m.group(1)) if m else 9
        if 0 <= hour <= 23:
            out["cron"] = f"0 {hour} * * *"
    elif "每周" in lower or "weekly" in lower:
        m = re.search(r"每周\s*[一二三四五六日天]?\s*(\d{1,2})?\s*点?", lower)
        out["cron"] = "0 9 * * 1"  # Monday 09:00 default
    elif "交易日收盘" in lower or "盘后" in lower:
        out["cron"] = "30 15 * * 1-5"  # 15:30 CST close
    elif "盘前" in lower:
        out["cron"] = "0 9 * * 1-5"

    # ── body_md ──────────────────────────────────────────────────
    # §Step3 — extract the free-text body from "create note" intents.
    # Patterns:
    #   "笔记：X" / "备注：X" / "memo: X" / "记一下 X" / "加一条笔记 X"
    # Without this slot, _note_create_args falls back to the entire
    # user_message (e.g. "给 600036 加一个笔记：哈哈打MVP") which is
    # far too verbose to store as a research note.
    body_match = (
        # §Step3 — fullwidth colon (U+FF1A "：") is the common separator
        # in Chinese ("笔记：内容"); bare ASCII colon ("memo: body")
        # is the English variant. The char class accepts both.
        # §Step3 — negative lookahead excludes ID-like tokens
        # (note-abc / alert-xyz / job-123) which appear in
        # delete/update queries. Without the lookahead those
        # queries are misclassified as body_md.
        re.search(r"(?:笔记|备注|memo|note)[::：\s]+(?!note-|alert-|job-|run-)(.+)$", message)
        or re.search(r"(?:记一下|做个笔记|做个备注|加个笔记|加一条?笔记|写个笔记|录一条?)\s+(.+)$", message)
    )
    if body_match:
        body = body_match.group(1).strip().strip('"').strip("'").strip()
        # §Step6.A — body_md gate: do not treat a bare limit phrase as
        # a note body. Without this, "600036 笔记 limit 3" routes to
        # (NOTE, CREATE) because "limit 3" gets captured as body_md and
        # the slot-aware override fires. Also reject empty / pure-punct
        # / number-only captures (e.g. "笔记 3").
        limit_only = re.match(r"^\s*(limit\s*)?\d+\s*(?:条|个|只|条记录|条笔记)?\s*$", body)
        near_limit = re.match(r"^\s*(?:前|最近)\s*\d+\s*(?:条|个|只)?\s*$", body)
        empty_body = not body or body.strip() in {"", ".", "。", ",", "，"}
        if empty_body:
            pass  # drop silently — do not set body_md
        elif limit_only or near_limit:
            pass  # §Step6.A — drop; user clearly meant a list query
        elif body and len(body) <= 2000:
            out["body_md"] = body

    # ── trade_date ───────────────────────────────────────────────
    # §Step3 — extract the trade date for run_trading_agents_analysis.
    # Patterns: "今天" / "今日" → today; "明天" / "明日" → tomorrow;
    # explicit "YYYY-MM-DD" or "YYYY/MM/DD".
    # §Step6.B — gate bare-ISO extraction on a keyword OR on the
    # absence of time_range. Without these gates, "2026-09-01 之后
    # 600036 的笔记" produces trade_date=2026-09-01 and routes to
    # (RUN, CREATE) even though the date is a *filter* on time_range
    # (since_ts). Accepted keywords: today / tomorrow / 分析日 /
    # 交易日 / 用 / 以. Bare-ISO fallback only fires when no
    # time_range has been extracted — Step 5.B's since_ts wins.
    today = _dt.date.today()
    if any(kw in message for kw in ("今天", "今日", "today")):
        out["trade_date"] = today.isoformat()
    elif any(kw in message for kw in ("明天", "明日", "tomorrow")):
        out["trade_date"] = (today + _dt.timedelta(days=1)).isoformat()
    elif "time_range" not in out:
        m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", message)
        if m:
            try:
                d = _dt.date.fromisoformat(m.group(1).replace("/", "-"))
                out["trade_date"] = d.isoformat()
            except ValueError:
                pass

    # ── research_depth ───────────────────────────────────────────
    # §Step3 — extract the research depth for run_trading_agents_analysis.
    # Patterns: "深度 N" / "N 层" / "depth N" / "深 N".
    m = re.search(r"(?:深度|depth)\s*[:：]?\s*(\d)", lower)
    if not m:
        m = re.search(r"(\d)\s*(?:层|级)", message)
    if not m:
        m = re.search(r"深\s*(\d)", message)
    if m:
        try:
            depth = int(m.group(1))
            if 1 <= depth <= 5:
                out["research_depth"] = depth
        except ValueError:
            pass

    return out

_TIER1_KEYWORDS = {"价格", "多少钱", "报价", "quote", "价格?", "price", "rsi", "换手", "成交", "行情", "股价", "现在", "today", "今日"}
_TIER2_KEYWORDS = {"估值", "分析", "对比", "compare", "估值合理性", "对比一下"}
_TIER3_KEYWORDS = {"深度", "综合", "详细", "全维度", "深度分析", "全面分析"}


@dataclass
class RouteResult:
    intent: Intent = Intent.UNKNOWN
    tier: Tier = Tier.PLAN_EXECUTE
    symbols: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    op: Op | None = None  # §N1 — primary (intent, op) pair from
                           # fast_route_with_op(). ShortCircuit uses
                           # this for single-intent Tier 1 routes.
    multi_pairs: list[tuple[Intent, Op]] = field(default_factory=list)
    # §N1 — populated by fast_route_with_op() when classify_multi()
    # detects 2+ read-only (intent, op) pairs in the same message.
    # ShortCircuit._run_multi reads this to know which tools to
    # invoke sequentially without going through Tier 2 / LLM.

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "tier": int(self.tier),
            "symbols": list(self.symbols),
            "confidence": self.confidence,
            "reason": self.reason,
            "op": self.op.value if self.op else None,
            "multi_pairs": [(i.value, o.value) for i, o in self.multi_pairs],
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
    Intent.SCHEDULED: ({"定时", "cron", "定时任务", "scheduled",
                      # §Step2 — natural-language schedule hints ("每天跑 X" /
                      # "每日触发" / "周期跑"). Combined with a CREATE verb
                      # ("跑" / "触发") these route to scheduled/CREATE;
                      # without a verb they stay scheduled/LIST (default).
                      "每天", "每日", "周期", "schedule", "scheduler", "cron-job",
                      # market-session phrases (暗示 schedule): 盘后 / 盘前 /
                      # 收盘后 / 开市前 / 收盘 / 开盘. Combined with a verb they
                      # route to SCHEDULED/CREATE; alone they stay SCHEDULED/LIST.
                      "盘后", "盘前", "收盘后", "开市前", "收盘", "开盘", "盘后跑", "盘前跑"}, Op.LIST),
    Intent.RUN:       ({"分析任务", "运行", "跑一下", "analyse", "analyze", "analysis", "run"}, Op.LIST),
    Intent.REPORT:    ({"分析报告", "报告", "report"}, Op.LIST),
}

_OP_KW: dict[Op, set[str]] = {
    Op.CREATE: {"新建", "创建", "添加", "加入", "新增", "写", "建", "create", "add",
                "schedule", "安排", "新建一个", "建一个", "做一个",
                # 口语化:"加一下 / 加个 / 加一条 / 加一个 / 加关注 / 加笔记"
                "加",   # bare 加 — most common Chinese verb for "add"
                "加一下", "加一个", "加个", "加一条", "加个新的",
                "记一下", "做个", "录入",
                # '跑一下 / 启动 / 跑起来' for analysis-run start; the
                # dispatch table maps (RUN, CREATE) to
                # run_trading_agents_analysis so these belong here.
                "跑一下", "跑起来", "跑个", "跑", "启动", "run-it", "开始",
                # Schedule-creation phrases (e.g. "每天早上 9 点跑 X" / "盘后跑 Y")
                # that imply the user wants to *create* a scheduled task.
                "每天跑", "每日跑", "定时跑", "周期跑", "按周期跑", "排个任务",
                # §Step3 — bare 备注 / 记一下 / 写个笔记 imply CREATE
                # without explicit 加/创建/添加. Without these,
                # "600036 备注：基本面强劲" routes to NOTE/LIST.
                "备注", "记一下", "写个笔记", "录一条", "录一下", "写一下",
                # §Step2 — write-operation hints ("提醒" / "提醒我" /
                # "提醒一下") that imply the user wants to *create* an
                # alert even when they don't say "添加" / "创建"
                # explicitly. Without these, classify() falls back to
                # ALERT/LIST and routes to list_alerts instead of
                # create_alert — making "价格超过 50 提醒 600036" a
                # no-op instead of a real pending_approval flow.
                "提醒", "提醒我", "提醒一下", "建一个提醒", "设置提醒",
                "加上", "设个", "给我建",
                # scheduled-task implicit creation: "盘后跑 X" / "每天
                # 早上 9 点跑 X" — the user wants to *create* a job,
                # not list existing ones.
                "盘后跑", "盘前跑", "收盘后跑", "开市前跑", "定时跑"},
    Op.LIST:   {"查看", "列出", "显示", "看看", "show", "list", "有哪些", "有什么",
                "全部的", "所有的", "列表"},
    Op.UPDATE: {"更新", "修改", "改", "调整", "edit", "update", "改一下",
                "改一下", "改成", "换一下", "替换"},
    Op.DELETE: {"删除", "移除", "去掉", "删", "delete", "remove", "取消关注", "停用",
                "关闭", "取消", "删掉",
                # 口语化:"删一下 / 删了 / 删掉全部 / 清掉"
                "删一下", "删了", "清掉", "清除", "清理"},
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

    # 0. §Step3 — slot-aware override FIRST. When extract_slots has
    # already produced structured params (trade_date / body_md / cron),
    # the user clearly wants to *create* a run / note / scheduled-task.
    # Apply the override before entity detection so "记一下 X 估值合理"
    # doesn't get its (NOTE, CREATE) routing hijacked by the COMPARE
    # legacy intent from the "估值" keyword. Without this override the
    # legacy COMPARE intent plus verb-driven CREATE produces
    # (COMPARE, CREATE) which has no _CRUD_DISPATCH entry — the LLM
    # has to re-derive the right tool.
    _slots = extract_slots(message or "")
    if "trade_date" in _slots or "research_depth" in _slots:
        return Intent.RUN, Op.CREATE
    if "body_md" in _slots:
        return Intent.NOTE, Op.CREATE

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

    # 3. Legacy fallback (read-only intents like QUOTE / NEWS / ANALYSIS).
    # §Step2 — even when no entity keyword matched, a write-op verb
    # ("跑" / "提醒" / "盘后跑" / etc.) should override the default
    # Op.READ fallback. Without this, queries like "盘后跑 600036"
    # fall through to read-only Tier 2 even though the user clearly
    # wants to *create* a scheduled task. The (intent=UNKNOWN, op=CREATE)
    # pair lets the orchestrator's _CRUD_DISPATCH short-circuit pick
    # the right tool (create_scheduled_task) without first resolving
    # an entity.
    legacy = classify_intent(message)
    for vop, vkws in _OP_KW.items():
        if any(vk in text for vk in vkws):
            return legacy, vop
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
    # §N1 — read-only multi-intent (e.g. "看一下笔记和告警",
    # "我的关注 + 600036 的笔记") can short-circuit too. All pairs
    # must be LIST or READ op AND Tier 1 read tools must exist for
    # every intent. Otherwise fall through to PLAN_EXECUTE (existing).
    if multi_intent:
        read_only_pairs = [
            p for p in multi_pairs
            if p[1] in (Op.LIST, Op.READ)
        ]
        # All pairs read-only AND at least 2 of them.
        if len(read_only_pairs) == len(multi_pairs) and len(read_only_pairs) >= 2:
            return RouteResult(
                intent=intent, tier=Tier.DIRECT, symbols=symbols,
                op=op,
                multi_pairs=list(multi_pairs),
                confidence=0.85,
                reason=f"read-only multi-intent ({len(read_only_pairs)} pairs) -> Tier 1",
            ), op
        # Mixed (read + write) or write-only multi-intent — Tier 2.
        return RouteResult(
            intent=intent, tier=Tier.PLAN_EXECUTE, symbols=symbols,
            op=op,
            confidence=0.85,
            reason=f"multi-intent ({len(multi_pairs)} pairs, mixed) -> Tier 2",
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
