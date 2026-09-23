"""§0.4.29 — LLM-based intent + op router.

Replaces the keyword-based :func:`tier.classify` as the **primary**
routing path. The LLM sees an intent catalog + op catalog and returns
strict JSON ``{"intent": ..., "op": ..., "confidence": ...}``. Falls
back to the legacy keyword :func:`tier.classify` on:

- LLM factory not configured (``is_configured()`` returns False)
- LLM call timeout / network error / 5xx
- JSON parse failure
- Validation failure (intent / op not in enum)

Design reference: deepseek-ai/deepseek-harness ("Everything is a Plugin")
uses function-calling to let the LLM pick the tool + args in one shot.
TradingAgents keeps the legacy CRUD dispatch + plan layer so the LLM
router only decides **what intent+op**, not which tool. The args factory
in ``_CRUD_DISPATCH`` still owns the args shape, which is the §7.3 #12
safety gate against LLM hallucination on write ops.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .tier import Intent, Op, classify as _keyword_classify

LOGGER = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Catalog (drives the prompt — keep in sync with tier.py enum)
# ----------------------------------------------------------------------

_INTENT_CATALOG: tuple[tuple[Intent, str], ...] = (
    # (intent, short description used in the prompt)
    (Intent.QUOTE,         "实时行情快照 — 最新价/涨跌/成交量等。例:\"600036 现在多少\"、\"看一下 NVDA 实时价\""),
    (Intent.HISTORY,       "历史 K 线 / N 天走势 — 蜡烛图数据。例:\"过去 30 天走势\"、\"日 K 线\""),
    (Intent.FUNDAMENTALS,  "基本面数据 — PE / 市值 / 营收 / EPS / ROE。例:\"AAPL 市盈率\"、\"招商银行基本面\""),
    (Intent.NEWS,          "新闻 / 公告 / 舆情。例:\"600036 最近有什么新闻\"、\"市场动态\""),
    (Intent.ALPHA,         "alpha 因子 / RankIC / 量价因子。例:\"算一下 alpha158\"、\"ROC / RSI 因子\""),
    (Intent.COMPARE,       "多标的横向对比 — 多 ticker + 对比关键词。例:\"对比 600036 和 600000\""),
    (Intent.ANALYSIS,      "综合分析 — 涉及多个维度 / 跨工具的复合查询。例:\"做完整分析\"、\"估值 + 新闻 + 走势\""),
    (Intent.WATCHLIST,     "关注列表 CRUD — 我的自选股。例:\"加入我的关注\"、\"我的关注列表\""),
    (Intent.NOTE,          "笔记 CRUD — 备注 / 记事。例:\"记一下\"、\"我的笔记\""),
    (Intent.ALERT,         "告警 CRUD — 价格提醒 / 监控告警。例:\"提醒我价格超过 50\"、\"我的告警\""),
    (Intent.SCHEDULED,     "定时任务 CRUD — 周期任务。例:\"每天盘后跑一次\"、\"新建定时任务\""),
    (Intent.RUN,           "分析任务状态查询 — run_id / 进行中。例:\"分析进度\"、\"取消任务\""),
    (Intent.REPORT,        "分析报告查询 — 报告内容 / 列表。例:\"看一下那份报告\"、\"列出报告\""),
    (Intent.UNKNOWN,       "无法明确归类 — fallback。"),
)

_OP_CATALOG: tuple[tuple[Op, str], ...] = (
    (Op.READ,         "只读查询 — list / get / show 等。"),
    (Op.LIST,         "列出多条记录 — 列表型查询(读)。"),
    (Op.CREATE,       "新建实体 — 写操作,会触发 HITL 确认。"),
    (Op.UPDATE,       "更新实体 — 写操作,会触发 HITL 确认。"),
    (Op.DELETE,       "删除单条记录 — 写操作,会触发 HITL 确认。"),
    (Op.BULK_DELETE,  "批量删除(同一标的下的所有) — 写操作,会触发 HITL 确认。"),
    (Op.RUN,          "立刻触发执行 — 跑一次定时任务 / 启动新分析。"),
)

# Build the system prompt once at import time. Pure derivation of the
# catalog, no LLM-specific tokens, so it's safe to cache.
def _build_system_prompt() -> str:
    intent_lines = "\n".join(
        f"- `{i.value}` — {desc}" for i, desc in _INTENT_CATALOG
    )
    op_lines = "\n".join(
        f"- `{o.value}` — {desc}" for o, desc in _OP_CATALOG
    )
    return (
        "你是一个意图分类路由器。\n"
        "读懂用户的中文/英文消息,只输出严格的 JSON,无 markdown,无注释。\n"
        "\n"
        "## Intent 可选值(intent)\n"
        f"{intent_lines}\n"
        "\n"
        "## Op 可选值(op)\n"
        f"{op_lines}\n"
        "\n"
        "## 规则\n"
        "1. 写操作(CRUD 新建/更新/删除)的 op 必须是 CREATE/UPDATE/DELETE/BULK_DELETE 之一。\n"
        "2. 只读查询的 op 必须是 READ 或 LIST。\n"
        "3. 含蓄表达(\"做一个 X\" / \"搞一个 Y\")**不要**默认 CREATE — 看上下文判断用户是要 "
        "分析(ANALYSIS+READ)还是要新建实体(只有当用户明确说\"建/加/删/提醒\"等动词且带实体名时才 CREATE)。\n"
        "4. 多标的(\"对比 X 和 Y\")+ 对比词 → COMPARE + READ。\n"
        "5. \"看一下 / 列出 / 我的\" → LIST。\n"
        "6. 跑一下 / 触发 / 立刻执行 → RUN。\n"
        "7. 无法判断 → intent=UNKNOWN, op=READ。\n"
        "\n"
        "## 输出格式(必须严格遵守)\n"
        "{\"intent\": \"<intent_value>\", \"op\": \"<op_value>\", \"confidence\": <0.0-1.0>}\n"
    )


SYSTEM_PROMPT: str = _build_system_prompt()


# ----------------------------------------------------------------------
# Route result + Router class
# ----------------------------------------------------------------------


@dataclass
class IntentRoute:
    """Final classification result consumed by the orchestrator."""

    intent: Intent
    op: Op
    confidence: float = 0.0
    source: str = "llm"        # "llm" | "keyword_fallback" | "cache"
    raw_response: str | None = None
    elapsed_ms: int = 0


# Strict JSON matcher — accept the first {...} block, ignore trailing
# junk (DeepSeek / OpenAI models occasionally add a closing sentence).
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\"intent\"[^{}]*\}")


class LLMIntentRouter:
    """LLM-backed intent + op classifier. Falls back to keyword on error.

    The router is **stateless** beyond its ``cache`` and ``llm_factory``
    references. The orchestrator creates one instance at construction
    time and reuses it across turns (cheap).

    Usage::

        router = LLMIntentRouter(llm_factory=orch.llm_factory)
        route = await router.route(user_message)
        # route.intent, route.op drive _CRUD_DISPATCH / short_circuit
    """

    # Quick model = fast + cheap. Intent classification is a low-stakes
    # call; "deep" would burn budget without quality benefit. Same shape
    # as the synthesizer's quick path.
    DEFAULT_MODE = "quick"
    DEFAULT_MAX_TOKENS = 80
    DEFAULT_TIMEOUT_S = 10.0

    def __init__(
        self,
        llm_factory: Any | None,
        cache: Any | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_S,
        enabled: bool = True,
    ) -> None:
        self._llm_factory = llm_factory
        self._cache = cache
        self._timeout = timeout_seconds
        self._enabled = enabled
        # Per-instance stats for observability (cheap dict, no lock —
        # single-threaded orchestrator per turn).
        self.stats: dict[str, int] = {
            "llm_calls": 0,
            "llm_failures": 0,
            "cache_hits": 0,
            "keyword_fallbacks": 0,
            "parse_failures": 0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def route(
        self,
        message: str,
        *,
        state: Any | None = None,
    ) -> IntentRoute:
        """Classify the user message into (Intent, Op).

        Returns a populated :class:`IntentRoute`. ``source`` indicates
        whether the result came from the LLM (``"llm"``), the response
        cache (``"cache"``), or the keyword fallback
        (``"keyword_fallback"``).

        Never raises — every error path falls back to keyword classify().
        """
        start = time.monotonic()
        message = (message or "").strip()
        if not message:
            intent, op = _keyword_classify("")
            return IntentRoute(
                intent=intent, op=op, confidence=0.0,
                source="keyword_fallback", raw_response=None,
                elapsed_ms=_elapsed_ms(start),
            )

        # 1. Cache lookup (cheap fast path)
        cache_key = self._cache_key(message)
        if self._cache is not None:
            cached = self._safe_cache_get(cache_key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                cached.source = "cache"
                cached.elapsed_ms = _elapsed_ms(start)
                return cached

        # 2. LLM disabled / factory not configured → keyword fallback
        if not self._enabled or self._llm_factory is None:
            self.stats["keyword_fallbacks"] += 1
            return self._keyword_fallback(message, start, reason="disabled")

        # 3. Real LLM call — distinguish LLM failures vs parse failures.
        # We use a single try with two except handlers so parse errors
        # (ValueError / unknown enum) bucket separately from LLM
        # provider errors (network / timeout / 5xx).
        try:
            route = await self._call_llm(message)
        except (ValueError, json.JSONDecodeError) as e:
            # _parse_response raises ValueError for malformed JSON,
            # unknown intent/op enum, etc. These are parse-level
            # failures, not LLM provider failures — separate bucket.
            self.stats["parse_failures"] += 1
            LOGGER.warning(
                "LLM intent router parse failed (%s: %s), falling back to keyword",
                type(e).__name__, e,
            )
            return self._keyword_fallback(message, start, reason=type(e).__name__)
        except Exception as e:
            # All other exceptions are LLM provider failures
            # (network / timeout / rate limit / 5xx / config).
            self.stats["llm_failures"] += 1
            LOGGER.warning(
                "LLM intent router failed (%s: %s), falling back to keyword classify()",
                type(e).__name__, e,
            )
            return self._keyword_fallback(message, start, reason=type(e).__name__)
        route.elapsed_ms = _elapsed_ms(start)
        self.stats["llm_calls"] += 1
        # 4. Cache the successful result for next turn
        if self._cache is not None:
            self._safe_cache_put(cache_key, route)
        return route

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _call_llm(self, message: str) -> IntentRoute:
        """Issue the LLM call. Raises on any failure (caller catches)."""
        factory = self._llm_factory
        if not getattr(factory, "is_configured", lambda: True)():
            # Treat as disabled path
            return self._keyword_fallback(message, _now_monotonic(), reason="unconfigured")
        # Try to use the router-specific slot; fall back to main factory.
        factory_for = getattr(self._llm_factory, "_factory_for", None)
        if callable(factory_for):
            try:
                provider = factory_for("intent_router").make(mode=self.DEFAULT_MODE)
            except Exception:
                provider = self._llm_factory.make(mode=self.DEFAULT_MODE)
        else:
            provider = self._llm_factory.make(mode=self.DEFAULT_MODE)

        # Build prompt with optional state context (carry-forward symbols
        # help disambiguate "看一下" → LIST(this ticker) vs LIST(all)).
        user_prompt = self._build_user_prompt(message)
        response = provider.complete_text(
            prompt=user_prompt,
            system=SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=self.DEFAULT_MAX_TOKENS,
        )
        content = getattr(response, "content", response) or ""
        return self._parse_response(content)

    def _build_user_prompt(self, message: str) -> str:
        return (
            "请分类下面的用户消息:\n"
            f"```\n{message}\n```\n"
            "只输出 JSON: {\"intent\": \"...\", \"op\": \"...\", \"confidence\": <0.0-1.0>}"
        )

    def _parse_response(self, content: str) -> IntentRoute:
        """Parse LLM JSON output → IntentRoute. Raises on parse/validate failure."""
        text = (content or "").strip()
        # Strip markdown fences if the model wraps the JSON.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        # Find the first JSON object in the response (defensive).
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            raise ValueError(f"no JSON object in response: {text[:200]!r}")
        payload = json.loads(match.group(0))
        intent_raw = payload.get("intent")
        op_raw = payload.get("op")
        conf_raw = payload.get("confidence", 0.5)
        if not intent_raw or not op_raw:
            raise ValueError(f"missing intent/op in response: {payload}")
        # Validate against enum (Pydantic-style Literal validation).
        # Case-insensitive: some LLMs (DeepSeek, GPT-4 in casual mode)
        # return enum names instead of values ("QUOTE" vs "quote").
        # Try the raw value first, then the enum name, then lower().
        def _resolve_intent(raw):
            raw = str(raw).strip()
            # raw value match
            try:
                return Intent(raw)
            except ValueError:
                pass
            # enum-name match (uppercase)
            try:
                return Intent[raw.upper()]
            except KeyError:
                pass
            # enum-name match (Title Case from enum)
            try:
                return Intent[raw.upper().replace(" ", "_")]
            except KeyError:
                pass
            raise ValueError(f"unknown intent {raw!r} (valid: {[i.value for i in Intent]})")

        def _resolve_op(raw):
            raw = str(raw).strip()
            try:
                return Op(raw)
            except ValueError:
                pass
            try:
                return Op[raw.upper()]
            except KeyError:
                pass
            try:
                return Op[raw.upper().replace(" ", "_")]
            except KeyError:
                pass
            raise ValueError(f"unknown op {raw!r} (valid: {[o.value for o in Op]})")

        intent = _resolve_intent(intent_raw)
        op = _resolve_op(op_raw)
        try:
            confidence = float(conf_raw)
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        return IntentRoute(
            intent=intent, op=op, confidence=confidence,
            source="llm", raw_response=text,
            elapsed_ms=0,
        )

    def _keyword_fallback(
        self, message: str, start: float, *,
        reason: str,
    ) -> IntentRoute:
        intent, op = _keyword_classify(message)
        self.stats["keyword_fallbacks"] += 1
        return IntentRoute(
            intent=intent, op=op, confidence=0.0,
            source="keyword_fallback", raw_response=None,
            elapsed_ms=_elapsed_ms(start),
        )

    # ------------------------------------------------------------------
    # Cache helpers (defensive — cache backend may not implement get/put)
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(message: str) -> str:
        # Hash the normalised message + catalog version so any catalog
        # change invalidates all entries. Catalog version = the
        # concatenated enum values (changes whenever Intent/Op gain a
        # value).
        catalog_version = (
            "|".join(i.value for i in Intent) + "#" +
            "|".join(o.value for o in Op)
        )
        h = hashlib.sha256()
        h.update(catalog_version.encode())
        h.update(b"\x00")
        h.update(message.lower().encode())
        return "intent_route:" + h.hexdigest()[:32]

    def _safe_cache_get(self, key: str) -> IntentRoute | None:
        try:
            v = self._cache.get(key)
        except Exception as e:
            LOGGER.debug("intent router cache.get failed: %s", e)
            return None
        return v if isinstance(v, IntentRoute) else None

    def _safe_cache_put(self, key: str, route: IntentRoute) -> None:
        try:
            self._cache.put(key, route)
        except Exception as e:
            LOGGER.debug("intent router cache.put failed: %s", e)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _now_monotonic() -> float:
    return time.monotonic()


__all__ = [
    "IntentRoute",
    "LLMIntentRouter",
    "SYSTEM_PROMPT",
]
