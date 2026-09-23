"""Tier 1 short-circuit (v2 spec D1 / N90 fix).

Routes a regex-matched "price?" query directly to ``tool.invoke()``
without touching the StateGraph. Three guarantees:

- 0 LLM calls (N87 fix)
- DataResponse.warnings emitted as ``warning`` SSE event (N90 fix)
- tier=2 fallback when tool raises — orchestrator picks up the next tier
"""
from __future__ import annotations

import logging
import re as _re
from typing import Any, AsyncIterator

from tradingagents.agent_harness.tools import ToolContext, ToolRegistry
from tradingagents.agent_harness.tools.schema import ToolSchema

from .tier import Intent, RouteResult, Tier
from .template import TemplateEngine, should_use_template  # noqa: F401

_LATEST_REPORT_HINT_KW = frozenset({
    "详情", "详细内容", "打开", "details", "detail", "view", "read",
    "内容", "看看这份", "这份", "刚才", "最新", "刚才那份", "最近那份",
})

LOGGER = logging.getLogger(__name__)


class ShortCircuit:
    """Server-side executor that bypasses the StateGraph for Tier 1 queries.

    v2 spec §D1 N101 fix — Tier 1b: when ``template_engine`` is wired and
    the query matches one of ``TEMPLATE_TRIGGER_KEYWORDS`` (e.g. "说明",
    "解释"), the tool result is rendered through a Jinja2 template and
    emitted as a text string instead of raw structured data.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        template_engine: "TemplateEngine | None" = None,
    ) -> None:
        self.registry = registry
        self.template_engine = template_engine

    async def run(
        self,
        route: RouteResult,
        message: str,
        context: ToolContext,
        slots: dict | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Stream SSE events for a Tier 1 query.

        Yields ``(event_name, payload)`` tuples. Caller maps to SSE wire format.

        §N1 — when ``route.multi_pairs`` is non-empty (set by
        fast_route_with_op() for read-only multi-intent), dispatches
        to :meth:`_run_multi` which invokes every tool sequentially
        and emits a single ``agent_final`` with
        ``result.multi = [...]``.
        """
        if route.multi_pairs and len(route.multi_pairs) >= 2:
            async for ev in self._run_multi(route, message, context, slots=slots):
                yield ev
            return

        # §0.4.23 — Tier 1 compare intent. When the user says "对比 A/B/C"
        # with 2+ symbols we fan out to one ``get_history`` call per symbol
        # (so each line gets a proper candlestick window), merge the
        # results, and emit a single ``agent_final`` with the
        # ``render_compare_card`` HTML. Falls back to PLAN_EXECUTE when
        # only 1 symbol is given (handled by the regular single-symbol
        # path below).
        if route.intent == Intent.COMPARE and len(route.symbols) >= 2:
            async for ev in self._run_compare(route, message, context, slots=slots):
                yield ev
            return

        symbol = route.symbols[0] if route.symbols else ""
        tool_name = self._tool_for_intent(route.intent, slots, symbol=symbol, message=message)
        if not tool_name:
            yield ("warning", {"message": f"no Tier 1 tool for intent={route.intent}"})
            return
        # §Step 18 — symbol-less queries reach Tier 1 when the slot
        # carries an identifier the tool can consume (currently just
        # ``report_id`` → ``get_report``). Without this, "读报告
        # run-55464f3..." emits "no ticker detected" and falls back to
        # Tier 2 where the LLM re-derives the right call (plan cache
        # can latch onto a bad args shape).
        if not symbol and not (slots or {}).get("report_id"):
            yield ("warning", {"message": "no ticker detected, falling back to Tier 2"})
            return

        try:
            tool = self.registry.get(tool_name)
            args_schema = tool.schema.args_schema
            args = self._build_args(args_schema, symbol, slots=slots, message=message)
            yield ("tool_call", {"name": tool_name, "args": self._safe_dump(args)})
            result = await tool.invoke(args, context)
            result_payload = self._safe_dump(result)
            # §0.4.25 — attach a backend-rendered friendly card so the
            # frontend can drop the duplicated ``formatRawResult`` pipe-
            # table path. Single source of truth = ``display_view_for``.
            result_payload = self._attach_display_html(tool_name, result_payload)
            yield ("tool_result", {"name": tool_name, "result": result_payload})
            # v2 spec §D1 N101 fix: Tier 1b — render template when query
            # matches ``TEMPLATE_TRIGGER_KEYWORDS``.  Falls back to raw
            # emit when no engine is wired or template name does not
            # match the intent.
            if self.template_engine is not None and should_use_template(message):
                tmpl_name = f"{route.intent.value}_simple"
                if self.template_engine.has(tmpl_name):
                    try:
                        text = self.template_engine.render(
                            tmpl_name, **self._ctx_for_template(result_payload),
                        )
                        yield ("agent_final", {
                            "tier": int(Tier.DIRECT),
                            "result": text,
                            "rendered": True,
                            "template": tmpl_name,
                        })
                        return
                    except Exception:
                        # Template render failed → fall through to raw emit
                        LOGGER.debug(
                            "template render failed, falling back to raw emit",
                            exc_info=True,
                        )
            # §Step 10 — surface the scope slot to the UI so the
            # result badge can read "my notes" / "all notes" rather than
            # a raw symbol filter. Safe default = "user".
            scope_hint = (slots or {}).get("scope", "user")
            # §Step 26 — when the tool result is a dict, render it
            # into a friendly markdown summary via the orchestrator's
            # _friendly_summary helper. We keep the raw payload in
            # ``result_raw`` so downstream consumers (audit / L3) still
            # see the structured data; ``result`` becomes the markdown
            # string the frontend renders via renderMarkdown().
            friendly = None
            if isinstance(result_payload, dict):
                # §Step 41 — prefer this ShortCircuit's own registry
                # metadata for the ``display_view`` lookup. Falling back
                # to ``Orchestrator._friendly_summary`` still works
                # (it consults ``get_default_tool_registry``), but in
                # tests / sandbox the default registry is empty so the
                # only way to render alpha / quote / news / etc. nicely
                # is to read the tool's registered metadata here.
                view_key = None
                try:
                    t = self.registry.get(tool_name)
                    view_key = t.schema.metadata.get("display_view")
                except Exception:
                    view_key = None
                if view_key:
                    try:
                        from tradingagents.agent_harness.tools.display_view import (
                            display_view_for,
                        )
                        friendly = display_view_for(result_payload, intent=view_key)
                    except Exception:
                        friendly = None
                if friendly is None:
                    try:
                        from .orchestrator import Orchestrator as _O
                        friendly = _O._friendly_summary(
                            result_payload, tool_name=tool_name,
                        )
                    except Exception:
                        friendly = None
            yield ("agent_final", {
                "tier": int(Tier.DIRECT),
                "result": friendly if friendly is not None else result_payload,
                "result_raw": result_payload,
                "tool_name": tool_name,
                "scope": scope_hint,
            })
        except Exception as e:
            LOGGER.warning("Tier 1 short-circuit failed: %s", e)
            yield ("error", {"tier": int(Tier.DIRECT), "error": str(e)})

    async def _run_compare(
        self,
        route: RouteResult,
        message: str,
        context: ToolContext,
        slots: dict | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """§0.4.23 — fan-out ``get_history`` for every symbol in
        ``route.symbols`` and merge into a compare card.

        Each per-symbol tool_call / tool_result is yielded so the
        reasoning trace still shows the user what's happening. The
        final ``agent_final`` carries the rendered HTML in
        ``result`` (string) so the frontend's harness.js detects the
        ``<div class="compare-card">`` prefix and emits it as raw
        HTML (not markdown).
        """
        from tradingagents.agent_harness.renderers.history_sparkline import infer_history_params
        from tradingagents.agent_harness.renderers.compare_sparkline import render_compare_card

        tool = self.registry.get("get_history")
        args_schema = tool.schema.args_schema
        # Honor explicit interval / lookback_days slots when present.
        interval, lookback = infer_history_params(message or "")
        per_call_slots = dict(slots or {})
        if "interval" not in per_call_slots:
            per_call_slots["interval"] = interval
        if "lookback_days" not in per_call_slots:
            per_call_slots["lookback_days"] = lookback

        series: list[dict] = []
        for sym in route.symbols:
            try:
                args = self._build_args(args_schema, sym, slots=per_call_slots)
            except Exception as e:
                yield ("warning", {"message": f"compare build_args {sym} failed: {e}"})
                continue
            yield ("tool_call", {"name": "get_history", "args": self._safe_dump(args)})
            try:
                result = await tool.invoke(args, context)
            except Exception as e:
                yield ("tool_result", {"name": "get_history", "result": self._attach_display_html("get_history", {"symbol": sym, "error": str(e)})})
                continue
            payload = self._safe_dump(result)
            yield ("tool_result", {"name": "get_history", "result": self._attach_display_html("get_history", payload)})
            candles = payload.get("candles") or []
            closes = [c.get("close") for c in candles if c.get("close") is not None]
            series.append({
                "symbol": payload.get("symbol", sym),
                "name": payload.get("name") or "",
                "exchange": payload.get("exchange") or "",
                "currency": payload.get("currency") or "",
                "closes": closes,
            })

        title = f"对比 {' / '.join(s.get('symbol', '?') for s in series)}"
        rendered = render_compare_card(series, title=title)
        yield ("agent_final", {
            "tier": int(Tier.DIRECT),
            "intent": route.intent.value,
            "tool_name": "get_history",
            "result": rendered,
            "rendered": True,
            "symbols": [s.get("symbol") for s in series],
            "summary": f"对比 {len(series)} 只标的走势",
        })


    async def _run_multi(
        self,
        route: RouteResult,
        message: str,
        context: ToolContext,
        slots: dict | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """§N1 — read-only multi-intent: invoke every (intent, op)
        pair sequentially and emit one combined ``agent_final`` with
        ``result.multi = [...]``. 0 LLM calls.

        §Step4 — accepts ``slots`` so read tools honour user-side
        limit / include_disabled overrides.

        Each tool's tool_call + tool_result events are forwarded so the
        frontend reasoning trace sees every step. If a tool raises,
        we emit a ``warning`` event and continue to the next pair (do
        not abort the whole multi-intent batch).
        """
        results: list[dict[str, Any]] = []
        for intent, op in route.multi_pairs:
            symbol = route.symbols[0] if route.symbols else ""
            tool_name = self._tool_for_intent(intent, slots, symbol=symbol, message=message)
            if not tool_name:
                yield ("warning", {
                    "message": f"no Tier 1 tool for intent={intent.value}",
                })
                continue
            try:
                tool = self.registry.get(tool_name)
                args_schema = tool.schema.args_schema
                args = self._build_args(args_schema, symbol, slots=slots, message=message)
                yield ("tool_call", {
                    "name": tool_name,
                    "args": self._safe_dump(args),
                })
                result = await tool.invoke(args, context)
                result_payload = self._safe_dump(result)
                yield ("tool_result", {
                    "name": tool_name,
                    "result": result_payload,
                })
                results.append({
                    "intent": intent.value,
                    "op": op.value,
                    "tool": tool_name,
                    "result": result_payload,
                })
            except Exception as e:
                LOGGER.warning(
                    "Tier 1 multi-intent tool failed: %s %s",
                    tool_name, e,
                )
                yield ("warning", {
                    "message": f"multi-intent tool {tool_name} failed: {e}",
                    "tool": tool_name,
                })
                # Continue with next pair — partial results are better
                # than aborting the whole batch.
                continue

        # §Step5.C — multi-read aggregation. When 2+ read tools run in
        # one short-circuit turn, render a single one-paragraph summary
        # so the UI shows coherent prose instead of a JSON blob. Falls
        # back to concatenating the raw tool text when no count field
        # is available.
        summary_text = self._summarise_multi(results)
        yield ("agent_final", {
            "tier": int(Tier.DIRECT),
            "result": {"multi": results, "count": len(results)},
            "summary": summary_text,
            "rendered": True,
            "multi_intent": True,
        })


    @staticmethod
    def _summarise_multi(results: list[dict[str, Any]]) -> str:
        """§Step5.C — turn a list of read-tool results into one prose paragraph.

        Each result is ``{intent, op, tool, result: {text, count, ...}}``.
        Falls back to ``"\n".join(text)`` when count fields are absent
        so we never return an empty string for a successful batch.
        """
        if not results:
            return ""
        lines: list[str] = []
        total = 0
        for r in results:
            payload = r.get("result") or {}
            if isinstance(payload, dict):
                text = str(payload.get("text", "")).strip()
                count = payload.get("count")
            else:
                text = str(payload).strip()
                count = None
            if count is not None:
                try:
                    total += int(count)
                except (TypeError, ValueError):
                    pass
            intent_cn = {
                "list_notes": "笔记",
                "list_alerts": "告警",
                "list_reports": "报告",
                "list_runs": "分析任务",
                "list_scheduled_tasks": "定时任务",
                "watchlist": "关注",
            }.get(r.get("intent", ""), r.get("intent", ""))
            head = f"• {intent_cn}: " if intent_cn else "• "
            if count is not None:
                head += f"{count} 条"
            else:
                head += text.splitlines()[0] if text else "(无内容)"
            lines.append(head)
        summary = "\n".join(lines)
        if total:
            summary = f"共 {total} 条记录:\n" + summary
        return summary

    def _safe_dump(self, obj: Any) -> Any:
        """Project a tool output into a JSON-serialisable form.

        §Step 8 — when ``obj`` exposes ``display_view()``, prefer it
        over ``model_dump()`` so the UI receives a small UI-safe dict
        (summary / preview / count) instead of every internal field.
        Tools that don't declare ``display_view`` keep using the
        raw dump — backward compatible.
        """
        view = getattr(obj, "display_view", None)
        if callable(view):
            try:
                return view()
            except Exception:
                pass  # fall through to model_dump
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        return obj


    # §0.4.25 — tool-name → Capability/intent mapping. Centralised so
    # every ``tool_result`` emit site uses the same intent string and
    # the friendly-card renderer is the single source of truth.
    _TOOL_INTENT_MAP: dict[str, str] = {
        "get_quote": "quote",
        "get_history": "history",
        "get_fundamentals": "fundamentals",
        "get_news": "news",
        "list_alpha_factors": "alpha",
        "list_reports": "list",
        "list_runs": "list",
        "list_watchlist": "list",
        "list_notes": "list",
        "list_alerts": "list",
        "list_scheduled_tasks": "list",
        "get_report": "report_read",
        "create_note": "ack",
        "update_note": "ack",
        "delete_note": "ack",
        "create_alert": "ack",
        "update_alert": "ack",
        "delete_alert": "ack",
        "add_watchlist": "ack",
        "remove_watchlist": "ack",
        "create_scheduled_task": "ack",
        "update_scheduled_task": "ack",
        "delete_scheduled_task": "ack",
        "run_trading_agents_analysis": "ack",
    }

    def _attach_display_html(self, tool_name: str, payload: Any) -> Any:
        """§0.4.25 — render ``payload`` through ``display_view_for`` and
        attach ``display_html`` so the frontend can skip its own
        ``formatRawResult`` pipe-table duplication.

        Only attaches when payload is a dict (most tool results).
        Renderer failures never crash the SSE stream — graceful fallback
        to the raw payload.
        """
        if not isinstance(payload, dict):
            return payload
        try:
            from tradingagents.agent_harness.tools.display_view import display_view_for
            intent = self._TOOL_INTENT_MAP.get(tool_name, "")
            if intent:
                html = display_view_for(payload, intent=intent)
                if isinstance(html, str) and "<div" in html:
                    payload["display_html"] = html
        except Exception:
            pass
        return payload

    def _ctx_for_template(result: Any) -> dict:
        """Flatten a tool result (dict / dict-like / Pydantic) into a
        template context dict.  Templates only see primitive values.
        """
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        if isinstance(result, dict):
            return {k: v for k, v in result.items()
                    if isinstance(v, (int, float, str, list, bool, type(None)))}
        # Fallback: wrap as a single ``value`` field
        return {"value": str(result)}

    @staticmethod
    def _wants_latest_report(message: str) -> bool:
        """True when the user clearly wants to open a specific (latest)
        report but didn't name the id by token. Used to short-circuit
        straight to get_report(latest) without listing first.
        """
        if not message:
            return False
        lower = message.lower()
        # Explicit id already handled by slot extraction; we only step in
        # when the user wants detail but didn't paste a run-/report- token.
        if _re.search(r"run-[a-f0-9]{12,}", message, _re.IGNORECASE):
            return False
        if _re.search(r"report-[a-z0-9_-]+", message, _re.IGNORECASE):
            return False
        return any(kw in lower for kw in _LATEST_REPORT_HINT_KW)

    @staticmethod
    def _resolve_latest_report_id(symbol: str) -> str | None:
        """Return the most recent report_id from history.

        Prefers ``symbol`` when provided; falls back to overall latest.
        Returns ``None`` when the index has no reports at all.

        Resolution order:
          1. The LangChain-tool side singleton injected by
             ``tools/impl.py:set_report_history`` — same store the
             list_reports / get_report tools read from.
          2. A fresh ``ReportHistory`` over the active results_dir as
             a last-resort fallback (covers tests + paths where the
             setter has not been called).
        """
        history = None
        try:
            from tradingagents.agent_harness.tools.impl import _get_report_history
            history = _get_report_history()
        except Exception:
            pass
        if history is None:
            try:
                from web.history import ReportHistory
                history = ReportHistory()
            except Exception:
                return None
        try:
            records = history.list_reports() or []
        except Exception:
            return None
        if not records:
            return None
        target = (symbol or "").strip().upper()
        if target:
            for r in records:
                if str(r.get("ticker", "")).upper() == target:
                    return r.get("report_id")
        return records[0].get("report_id")

    @staticmethod
    def _tool_for_intent(
        intent: Intent,
        slots: dict | None = None,
        symbol: str = "",
        message: str = "",
    ) -> str:
        # §7.3 #12 — every read-capable intent gets a default Tier 1
        # read tool so we never emit "no Tier 1 tool for intent=NOTE"
        # warnings. Write intents still go through Tier 2 (CRUD
        # dispatch → HITL gate) because they need approval.
        # §Step 16 — when a report_id slot is present, route to
        # get_report (which fetches full markdown) instead of
        # list_reports (which only returns metadata).
        # §Step 41 — alpha intent: when a symbol is present, route
        # to ``compute_alpha_factors`` (real numeric values) instead of
        # ``list_alpha_factors`` (just the factor name catalogue).
        # Without this, "算一下 600036.SS 的 alpha158 因子" silently
        # fell through to the name list.
        if intent == Intent.ALPHA and symbol:
            return "compute_alpha_factors"
        if intent == Intent.REPORT:
            # §Step 42 — when report_id slot is present, full-detail view.
            if slots and "report_id" in slots:
                return "get_report"
            # §Step 42 — when op is READ but no report_id (e.g. user typed
            # '看一下这份报告的详情' without naming the id), resolve the
            # most recent report from history. Prefer the symbol the
            # user mentioned; fall back to overall latest. Without
            # this the user gets a list_reports recap instead of the
            # full markdown they asked for.
            if (slots or {}).get("_report_read") or ShortCircuit._wants_latest_report(message):
                rid = ShortCircuit._resolve_latest_report_id(symbol)
                if rid:
                    if slots is None:
                        slots = {}
                    slots["report_id"] = rid
                    return "get_report"
        return {
            Intent.QUOTE: "get_quote",
            Intent.HISTORY: "get_history",
            Intent.FUNDAMENTALS: "get_fundamentals",
            Intent.NEWS: "get_news",
            Intent.ALPHA: "list_alpha_factors",
            Intent.WATCHLIST: "list_watchlist",
            Intent.NOTE: "list_notes",
            Intent.ALERT: "list_alerts",
            Intent.SCHEDULED: "list_scheduled_tasks",
            Intent.RUN: "list_runs",
            Intent.REPORT: "list_reports",
        }.get(intent, "")

    @staticmethod
    def _build_args(args_schema: type, symbol: str, slots: dict | None = None, message: str | None = None) -> Any:
        """Instantiate the tool's args schema with sensible defaults.

        §Step4 — merge ``state.slots`` (limit / threshold / ...) on top
        of the symbol-only defaults so Tier 1 read paths honour the
        user's structured params the same way _list_*_args does for
        Tier 2. Unknown slot keys are silently dropped — the args
        schema is the source of truth for which fields are valid.
        """
        # Lazy imports to avoid circulars.
        from tradingagents.agent_harness.tools import builtin as _builtin  # noqa: F401
        # §Step 18 — when no symbol is present but a slot such as
        # ``report_id`` carries the necessary id, we still want to
        # invoke the tool. Build a minimal payload from the slot and
        # let the schema validator ignore unknown keys. Symbol stays
        # optional for tools that don't need it (GetReportArgs only
        # declares ``report_id``).
        payload: dict[str, Any] = {}
        if symbol:
            payload["symbol"] = symbol
        elif (slots or {}).get("report_id"):
            payload["report_id"] = slots["report_id"]
        # §Step4 — apply safe slots only. We don't pass threshold /
        # body_md / cron / trade_date to read tools (they don't accept
        # those fields). Only ``limit`` and ``time_range`` flow through
        # to read_* args.
        if slots:
            # §0.4.19 — allow ``lookback_days`` / ``interval`` slots for
            # HistoryArgs so explicit overrides win over message-derived
            # defaults. We gate by schema field to keep the safe list
            # tight.
            for k in ("limit", "include_disabled", "lookback_days", "interval"):
                if k in slots and k in getattr(args_schema, "model_fields", {}):
                    payload[k] = slots[k]
            # §Step 16 — forward report_id slot to GetReportArgs so the
            # single-intent Tier 1 path can call get_report instead of
            # list_reports when the user names a specific report.
            if "report_id" in slots and "report_id" in getattr(args_schema, "model_fields", {}):
                payload["report_id"] = slots["report_id"]
            # §Step5.B — convert ``time_range`` (ISO strings) into
            # ``since_ts / until_ts`` (epoch seconds). Only emit when
            # the target schema declares either field — otherwise the
            # slot is silently dropped (e.g. watchlist has no time
            # window).
            tr = slots.get("time_range")
            if tr:
                try:
                    import datetime as _dt
                    since_iso, until_iso = tr
                    if since_iso and "since_ts" in getattr(args_schema, "model_fields", {}):
                        payload["since_ts"] = int(_dt.datetime.fromisoformat(since_iso).timestamp())
                    if until_iso and "until_ts" in getattr(args_schema, "model_fields", {}):
                        payload["until_ts"] = int(_dt.datetime.fromisoformat(until_iso).timestamp())
                except Exception:
                    pass
        # §0.4.19 — for HistoryArgs, derive ``lookback_days`` from the
        # caller's message when it isn't already in slots. Keeps Tier 1
        # short-circuit (``get_history``) honest about "最近 30 天" / "3
        # 个月" — without this, the user sees 20 bars no matter what
        # they asked for.
        if message and "lookback_days" not in payload and "HistoryArgs" in getattr(args_schema, "__name__", ""):
            try:
                from tradingagents.agent_harness.renderers.history_sparkline import infer_history_params
                interval, lookback = infer_history_params(message)
                if "interval" in getattr(args_schema, "model_fields", {}):
                    payload.setdefault("interval", interval)
                if "lookback_days" in getattr(args_schema, "model_fields", {}):
                    payload["lookback_days"] = lookback
            except Exception:
                pass

        if hasattr(args_schema, "model_validate"):
            try:
                return args_schema.model_validate(payload)
            except Exception:
                pass
        # Fallback: construct dataclass-like or dict.
        try:
            return args_schema(**payload)
        except Exception:
            try:
                return args_schema(symbol=symbol)
            except Exception:
                return payload
