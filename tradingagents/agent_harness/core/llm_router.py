"""§0.4.30 — DeepSeek-style LLM plan router.

DeepSeek-Harness pattern: skip the (intent, op) intermediate layer.
The LLM sees the tool catalog and the user's message and emits the
plan directly as a JSON array of ``{tool, args, parallel_group?}``.

Output shape (the only contract this module exposes):

.. code-block:: python

    [
        {"tool": "get_quote", "args": {"symbol": "600036.SS"}},
        {"tool": "get_news", "args": {"symbol": "600036.SS"}},
        # parallel_group is optional; when omitted, the orchestrator
        # defaults all read-only tools to group=0 and write tools to
        # their own groups (serial).
    ]

For A-class questions (date / concept / math), the router returns
``[]`` and the synthesizer answers directly. The LLM is told this
explicitly in the system prompt.

Failure modes:

- LLM factory not configured -> empty plan + ``fallback_reason``.
- LLM timeout / 5xx / parse error -> keyword fallback
  (``tier.classify`` + ``Orchestrator._CRUD_DISPATCH``) which
  delegates to ``Orchestrator._plan`` for the legacy heuristic. The
  keyword path is preserved verbatim from §0.4.29 so §0.4.28's
  ``"做一个"`` removal stays effective.
- LLM hallucinated a tool name not in the catalog -> the call is
  dropped, the rest of the plan still runs.
- LLM hallucinated args that fail Pydantic coercion -> the call is
  dropped, the rest of the plan still runs.

Cache: short-lived in-memory + persistent plan cache (so repeat
queries inside the same 5-min window skip the LLM).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .llm_catalog import ToolCatalogEntry, catalog_to_prompt

LOGGER = logging.getLogger(__name__)


# §0.4.31 — deterministic A-class short-circuit. The system prompt
# already tells the LLM to return ``[]`` for date/weekday/concept
# queries, but in practice the LLM still hallucinates get_quote calls
# when the session carries a focus asset (e.g. user asks "今天周几"
# and the LLM over-emits 6 banking peers). This pattern list catches
# the obvious cases BEFORE the LLM call — no tool, no LLM cost.
#
# The list is intentionally narrow: it must NOT fire on messages with
# any ticker or analysis verb. "今天 600036 收盘价" stays B-class.
_A_CLASS_PATTERNS: tuple[str, ...] = (
    # weekday / date
    r"^今天\s*(周几|星期几|几号|日期|几月几号)\s*[?]?$",
    r"^周几\s*[?]?$",
    r"^星期几\s*[?]?$",
    r"^(今天|现在)\s*(几\s*号|几\s*月|日期)\s*[?]?$",
    r"^今天\s*是\s*(\d{4}年)?(\d{1,2}月)?(\d{1,2}日)?.*[?]?$",
    # time
    r"^(现在|当前)\s*(几点|几点钟|时间)\s*[?]?$",
    r"^几\s*点(了)?\s*[?]?$",
    # weekend / weekday check
    r"^(今天|明天|昨天|后天)\s*(周|星期|是\s*周|是\s*星期).*[?]?$",
    r"^(是\s*|今天\s*)?(周\s*末|星期\s*末|周末|周末了吗|周末了没|今天\s*周末吗)\s*[?]?$",
    # weather / small talk
    r"^(今天|现在)\s*(天气|气温)\s*(如何|怎么样|怎样)?\s*[?]?$",
    # English equivalents
    r"^(what\s*day(\s*is\s*it)?\s*(today)?|what\s*is\s*today(\s*\'s\s*date)?)\s*[?]?$",
    r"^(what\s*time(\s*is\s*it)?|current\s*time)\s*[?]?$",
    r"^(is\s*it\s*)(weekend|saturday|sunday|monday)\s*(yet|now|today)?\s*[?]?$",
)
_A_CLASS_RE = re.compile("|".join(_A_CLASS_PATTERNS), re.IGNORECASE)

# Re-use the keyword extractor's analysis-verb list so this gate
# stays in sync with tier.py's own has_analysis_ctx check.
_ANALYSIS_VERBS = (
    "分析", "研究", "估值", "评估", "跑", "深度", "行情",
    "走势", "compare", "evaluate", "analyze", "analyse",
    "valuation", "深度分析",
)


def _is_pure_a_class(message: str, carry_symbols: list[str] | None) -> bool:
    """Return True iff the message is a pure date/weekday/time question
    with NO financial context (no ticker, no analysis verb, no carry).
    """
    msg = (message or "").strip()
    if not msg:
        return False
    # Carry-forward symbols are an anchor — if the prior turn named
    # 600036 and the user says "今天周几", we still treat it as pure
    # A-class (the carry is just conversational, not financial).
    # But if the user wrote "今天" + a ticker, that's B-class.
    if not _A_CLASS_RE.search(msg):
        return False
    # Strip matched A-class tokens to look for residual financial context.
    residual = _A_CLASS_RE.sub("", msg).strip(" ,，.。?？!！")
    if not residual:
        return True
    # If residual contains an analysis verb, this is NOT a pure A-class query.
    low = residual.lower()
    if any(kw in residual or kw in low for kw in _ANALYSIS_VERBS):
        return False
    # If residual looks like a ticker (digits + letters + . + length 4-12),
    # treat as B-class.
    if re.search(r"\b[A-Z0-9]{1,6}(?:\.[A-Z]{1,3})?\b", residual):
        return False
    # If carry_symbols is non-empty AND residual references it ("这个",
    # "它", "the one"), keep B-class.
    if carry_symbols:
        for ref in ("这个", "那只", "它", "this one", "that one", "it"):
            if ref in residual:
                return False
    return True


# ----------------------------------------------------------------------
# Plan data classes
# ----------------------------------------------------------------------


@dataclass
class ToolCall:
    """One LLM-emitted tool call. Args are JSON-ready (after validation)."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    parallel_group: int = 0
    # Filled when the router dropped a call (unknown tool, bad args).
    dropped_reason: str | None = None


@dataclass
class RouterPlan:
    """Plan emitted by :class:`LLMRouter`.

    - ``calls`` is the validated, deduplicated plan ready for the
      executor.
    - ``fallback_reason`` is non-None when the LLM failed and the
      keyword path kicked in.
    - ``source`` is one of ``"llm"``, ``"cache"``, ``"keyword_fallback"``,
      ``"empty"`` (A-class / system-level).
    - ``requires_confirmation`` is True when at least one emitted tool
      is a write tool (create_*/update_*/delete_*/add_to_*/remove_from_*/
      run_trading_agents_analysis). The synthesizer uses this to
      phrase the answer as a proposal rather than a completed
      action — the orchestrator surfaces the actual HITL prompt
      separately. Set by :meth:`LLMRouter._mark_writes` after parse.
    """

    calls: list[ToolCall]
    source: str
    fallback_reason: str | None = None
    elapsed_ms: int = 0
    confidence: float = 0.0
    requires_confirmation: bool = False


# ----------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class LLMRouter:
    """DeepSeek-style single-call plan router.

    Replaces the two-step (intent+op router -> plan LLM) flow from
    §0.4.29 with one LLM call that emits the plan array directly.
    """

    # System prompt — the LLM's only contract surface. Keep tight so
    # the catalog stays inside the model's context window.
    _SYSTEM_PROMPT = (
        "You are a plan router for a financial-analysis agent. "
        "Read the user's message and the available tools, then output a "
        "JSON plan array (no markdown, no commentary).\n"
        "\n"
        "Output schema — pick one of:\n"
        "1. Plan array: ``[{\"tool\": \"<name>\", \"args\": {...}}, ...]``\n"
        "   - Optional ``parallel_group`` int (default 0). Same group = "
        "concurrent.\n"
        "2. Empty array ``[]`` for A-class questions (date/weekday/concept/clarification) — "
        "the synthesizer will answer directly without calling any tool.\n"
        "\n"
        "Rules:\n"
        "- Pick ONLY tools listed in the catalog below. Tool names must "
        "match exactly.\n"
        "- Fill args with the values the user gave (symbols, ids, "
        "thresholds). When the user didn't name a symbol but the prior "
        "turn did, use the carried-forward symbol.\n"
        "- Independent calls (no data dependency between them) should "
        "share a parallel_group; dependent calls (one feeds the next) "
        "should be in different groups with the later group declaring "
        "the dependency.\n"
        "- Multi-source fan-out (quote + news + history for the same "
        "symbol) belongs in the same parallel_group.\n"
        "- Write tools (create_* / update_* / delete_* / add_to_* / "
        "remove_from_*) are HITL gated — the orchestrator auto-prompts "
        "the user. You can include them in the plan.\n"
        "- A-class (date / concept / clarification / weekday) returns "
        "``[]``. Don't call a tool just to answer a simple question.\n"
        "- Multi-intent queries (\"做完整分析：基础面+新闻+走势\") should "
        "fan out to multiple tools in one parallel group.\n"
        "\n"
        "—— Read-only vs write intent (strictly enforced):\n"
        "When the user asks for ANALYSIS / MONITORING / REVIEW / COMPARE / "
        "完整分析 / 看看 / 对比 / 走势 / 估值 / "
        "新闻 / 同业 / 监控告警建议 / 推荐参数, "
        "the plan MUST be read-only: only list_* / get_* / compute_* "
        "tools. Do NOT include create_*, update_*, delete_*, add_to_*, "
        "remove_from_*, run_trading_agents_analysis in such plans, even "
        "if the user says 推荐 / 建议 / 可以创建. The synthesizer "
        "phrases recommendations as suggestions; the orchestrator surfaces "
        "create_alert / etc. separately with HITL when the user EXPLICITLY "
        "confirms (e.g. 创建 / 新建 / 设置 / 下单 / 加入 / "
        "启动分析 / 跑一下).\n"
        "\n"
        "Tool catalog:\n"
        "{catalog}\n"
        "\n"
        "Output ONLY the JSON array, nothing else."
    )

    def __init__(
        self,
        *,
        llm_factory: Any | None,
        catalog: list[ToolCatalogEntry],
        cache: Any | None = None,
        max_catalog_chars: int = 18000,
    ) -> None:
        self._llm_factory = llm_factory
        self._catalog = catalog
        self._cache = cache
        self._max_catalog_chars = max_catalog_chars
        self._catalog_text = self._render_catalog()
        self._known_tools = {entry.name for entry in catalog}
        self._args_schema = {
            entry.name: entry.parameters for entry in catalog
        }
        self.stats: dict[str, int] = {
            "llm_calls": 0,
            "cache_hits": 0,
            "fallbacks": 0,
            "dropped_calls": 0,
            "empty_plans": 0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def route(
        self,
        user_message: str,
        *,
        carry_symbols: list[str] | None = None,
        context: Any | None = None,
    ) -> RouterPlan:
        """Plan a single user turn.

        Returns a :class:`RouterPlan` with at least one of
        ``source in {"llm", "cache", "keyword_fallback", "empty"}``.
        Never raises on LLM failure — falls back to keyword.
        """
        start = time.monotonic()
        # 1. Cache hit short-circuit
        cache_key = self._cache_key(user_message, carry_symbols)
        if cache_key:
            cached = self._safe_cache_get(cache_key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return RouterPlan(
                    calls=cached,
                    source="cache",
                    elapsed_ms=_elapsed_ms(start),
                )

        # 1b. §0.4.31 — pure A-class short-circuit (date/weekday/time).
        # The system prompt already tells the LLM to return [] for these,
        # but the LLM still over-emits get_quote when carry_symbols is
        # non-empty. Catch the obvious cases BEFORE the LLM call.
        if _is_pure_a_class(user_message, carry_symbols):
            self.stats["a_class_short_circuits"] = (
                self.stats.get("a_class_short_circuits", 0) + 1
            )
            self.stats["empty_plans"] += 1
            LOGGER.debug(
                "LLMRouter A-class short-circuit: %r",
                (user_message or "")[:60],
            )
            return RouterPlan(
                calls=[],
                source="empty",
                fallback_reason="A-class (date/weekday/time) short-circuit",
                elapsed_ms=_elapsed_ms(start),
            )

        # 2. LLM not configured -> empty plan; orchestrator decides
        if self._llm_factory is None or not self._llm_factory.is_configured():
            self.stats["fallbacks"] += 1
            return RouterPlan(
                calls=[],
                source="empty",
                fallback_reason="LLM factory not configured",
                elapsed_ms=_elapsed_ms(start),
            )

        # 3. Single LLM call
        try:
            provider = self._llm_factory.make(mode="deep")
            system_prompt = self._SYSTEM_PROMPT.replace("{catalog}", self._catalog_text)
            user_prompt = self._build_user_prompt(
                user_message, carry_symbols=carry_symbols,
            )
            response = provider.complete_text(
                prompt=user_prompt,
                system=system_prompt,
                temperature=0.0,
            )
            content = getattr(response, "content", response) or ""
            self.stats["llm_calls"] += 1
            calls = self._parse_plan(content)
        except Exception as e:
            LOGGER.info("LLMRouter LLM call failed: %s — falling back to keyword", e)
            self.stats["fallbacks"] += 1
            return RouterPlan(
                calls=[],
                source="keyword_fallback",
                fallback_reason=f"{type(e).__name__}: {e}",
                elapsed_ms=_elapsed_ms(start),
            )

        # 4. Validate + cache
        calls = self._dedupe(calls)
        if cache_key:
            self._safe_cache_put(cache_key, calls)
        if not calls:
            self.stats["empty_plans"] += 1
        # §0.4.31 — mark whether the plan includes a write tool so
        # the synthesizer knows the user hasn't actually approved
        # the action yet. The orchestrator surfaces the HITL gate
        # separately; the answer must NOT claim success.
        requires_confirmation = self._plan_needs_confirmation(calls)
        return RouterPlan(
            calls=calls,
            source="llm",
            elapsed_ms=_elapsed_ms(start),
            confidence=0.9,
            requires_confirmation=requires_confirmation,
        )

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_plan(self, content: str) -> list[ToolCall]:
        """Parse LLM JSON output -> list of validated ToolCall."""
        text = (content or "").strip()
        # Strip markdown fences if the model wraps the JSON.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        # Find the first JSON array in the response (defensive).
        match = _JSON_ARRAY_RE.search(text)
        if not match:
            # Maybe the LLM returned an object with a "plan" key.
            obj_match = _JSON_OBJECT_RE.search(text)
            if obj_match:
                try:
                    payload = json.loads(obj_match.group(0))
                except Exception as e:
                    LOGGER.info("LLMRouter: failed to parse object: %s", e)
                    return []
                if isinstance(payload, dict):
                    return self._parse_plan(json.dumps(payload.get("plan") or []))
            LOGGER.info("LLMRouter: no JSON array in response: %s", text[:200])
            return []
        try:
            data = json.loads(match.group(0))
        except Exception as e:
            LOGGER.info("LLMRouter: JSON parse failed: %s — content: %s", e, text[:200])
            return []
        if not isinstance(data, list):
            return []
        out: list[ToolCall] = []
        for item in data:
            call = self._parse_call(item)
            if call is not None:
                out.append(call)
        return out

    def _parse_call(self, item: Any) -> ToolCall | None:
        """Validate one plan entry against the catalog."""
        if not isinstance(item, dict):
            return None
        tool = item.get("tool") or item.get("name")
        if not isinstance(tool, str):
            return None
        if tool not in self._known_tools:
            self.stats["dropped_calls"] += 1
            LOGGER.info("LLMRouter: dropped unknown tool %r", tool)
            return None
        raw_args = item.get("args") or {}
        if not isinstance(raw_args, dict):
            raw_args = {}
        # Best-effort coerce args against the tool's schema. We only
        # filter out clearly bogus values; we don't *construct* the
        # Pydantic instance here — the executor does that. This keeps
        # the router cheap and lets the executor own error reporting.
        clean_args = self._coerce_args(tool, raw_args)
        if clean_args is None:
            self.stats["dropped_calls"] += 1
            LOGGER.info("LLMRouter: dropped %r — args coerce failed: %r", tool, raw_args)
            return None
        group_raw = item.get("parallel_group", 0)
        try:
            group = int(group_raw)
        except (TypeError, ValueError):
            group = 0
        return ToolCall(tool=tool, args=clean_args, parallel_group=group)

    def _coerce_args(self, tool: str, raw: dict[str, Any]) -> dict[str, Any] | None:
        """Trim args to schema fields and drop values that obviously don't fit.

        Returns ``None`` when a required field is missing (the executor
        can also reject, but early-drop saves an LLM roundtrip on
        blatant mistakes).
        """
        schema = self._args_schema.get(tool, {}) or {}
        properties = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])
        clean: dict[str, Any] = {}
        for key, val in raw.items():
            if key in properties:
                clean[key] = val
            else:
                # Unknown field — silently drop. The executor's Pydantic
                # validation would reject it anyway, but ignoring here
                # lets partial plans still run.
                continue
        # Required field check
        missing = required - set(clean.keys())
        if missing:
            return None
        return clean

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _cache_key(
        self, user_message: str, carry_symbols: list[str] | None,
    ) -> str | None:
        if self._cache is None:
            return None
        h = hashlib.sha256()
        catalog_version = "|".join(sorted(self._known_tools))
        h.update(catalog_version.encode())
        h.update(b"\x00")
        h.update(user_message.lower().encode())
        if carry_symbols:
            h.update(b"\x01")
            h.update(",".join(sorted(carry_symbols)).encode())
        return "llm_router:" + h.hexdigest()[:32]

    def _safe_cache_get(self, key: str) -> list[ToolCall] | None:
        try:
            v = self._cache.get(key)
        except Exception as e:
            LOGGER.debug("LLMRouter cache.get failed: %s", e)
            return None
        if not isinstance(v, list):
            return None
        # Cheap shape check — never trust the cache blindly.
        out: list[ToolCall] = []
        for item in v:
            if isinstance(item, ToolCall):
                out.append(item)
        return out

    # §0.4.31 — write-tool detection (drives RouterPlan.requires_confirmation).
    _WRITE_TOOLS = frozenset({
        "create_alert", "update_alert", "delete_alert",
        "delete_alerts_for_symbol",
        "create_note", "update_note", "delete_note",
        "delete_notes_for_symbol",
        "add_to_watchlist", "remove_from_watchlist",
        "create_scheduled_task", "update_scheduled_task",
        "delete_scheduled_task", "delete_scheduled_tasks_for_symbol",
        "run_trading_agents_analysis", "cancel_analysis_run",
        "run_scheduled_task",
    })

    def _plan_needs_confirmation(self, calls: list[ToolCall]) -> bool:
        """Return True if any call in the plan is a write tool.

        The synthesizer treats requires_confirmation=True plans as
        proposals — the actual HITL gate is surfaced separately by
        the orchestrator. See §0.4.31.
        """
        return any(c.tool in self._WRITE_TOOLS for c in calls)

    def _safe_cache_put(self, key: str, calls: list[ToolCall]) -> None:
        try:
            self._cache.put(key, calls)
        except Exception as e:
            LOGGER.debug("LLMRouter cache.put failed: %s", e)

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------

    def _render_catalog(self) -> str:
        """Render the catalog, truncating if necessary to fit the LLM's window."""
        text = catalog_to_prompt(self._catalog)
        if len(text) > self._max_catalog_chars:
            # Truncate the largest entries first.
            text = text[: self._max_catalog_chars] + "\n... (truncated)"
        return text

    @staticmethod
    def _build_user_prompt(
        user_message: str, *, carry_symbols: list[str] | None,
    ) -> str:
        carry_line = ""
        if carry_symbols:
            carry_line = (
                f"\nCarry-forward symbols (from previous turn, the user "
                f"didn't name a symbol in this turn): {', '.join(carry_symbols)}"
            )
        return (
            f"User message: {user_message}\n"
            f"Current date: {_today_cst_str()}"
            f"{carry_line}\n\n"
            "Output the JSON plan array (or [])."
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _dedupe(self, calls: list[ToolCall]) -> list[ToolCall]:
        """Drop duplicate (tool, args) pairs the LLM sometimes emits."""
        seen: set[tuple[str, str]] = set()
        out: list[ToolCall] = []
        for call in calls:
            key = (call.tool, json.dumps(call.args, sort_keys=True, ensure_ascii=False))
            if key in seen:
                continue
            seen.add(key)
            out.append(call)
        return out


def _today_cst_str() -> str:
    from datetime import datetime, timezone, timedelta
    cst = timezone(timedelta(hours=8))
    now = datetime.now(cst)
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    return now.strftime("%Y-%m-%d") + f" (东八区时间 {weekday_cn})"


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


__all__ = [
    "LLMRouter",
    "ToolCall",
    "RouterPlan",
]
