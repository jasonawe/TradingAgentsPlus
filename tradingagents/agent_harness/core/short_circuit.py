"""Tier 1 short-circuit (v2 spec D1 / N90 fix).

Routes a regex-matched "price?" query directly to ``tool.invoke()``
without touching the StateGraph. Three guarantees:

- 0 LLM calls (N87 fix)
- DataResponse.warnings emitted as ``warning`` SSE event (N90 fix)
- tier=2 fallback when tool raises — orchestrator picks up the next tier
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from tradingagents.agent_harness.tools import ToolContext, ToolRegistry
from tradingagents.agent_harness.tools.schema import ToolSchema

from .tier import Intent, RouteResult, Tier
from .template import TemplateEngine, should_use_template  # noqa: F401

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

        tool_name = self._tool_for_intent(route.intent, slots)
        if not tool_name:
            yield ("warning", {"message": f"no Tier 1 tool for intent={route.intent}"})
            return

        symbol = route.symbols[0] if route.symbols else ""
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
            args = self._build_args(args_schema, symbol, slots=slots)
            yield ("tool_call", {"name": tool_name, "args": self._safe_dump(args)})
            result = await tool.invoke(args, context)
            result_payload = self._safe_dump(result)
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
            tool_name = self._tool_for_intent(intent, slots)
            if not tool_name:
                yield ("warning", {
                    "message": f"no Tier 1 tool for intent={intent.value}",
                })
                continue
            symbol = route.symbols[0] if route.symbols else ""
            try:
                tool = self.registry.get(tool_name)
                args_schema = tool.schema.args_schema
                args = self._build_args(args_schema, symbol, slots=slots)
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

    @staticmethod
    def _safe_dump(obj: Any) -> Any:
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

    @staticmethod
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
    def _tool_for_intent(intent: Intent, slots: dict | None = None) -> str:
        # §7.3 #12 — every read-capable intent gets a default Tier 1
        # read tool so we never emit "no Tier 1 tool for intent=NOTE"
        # warnings. Write intents still go through Tier 2 (CRUD
        # dispatch → HITL gate) because they need approval.
        # §Step 16 — when a report_id slot is present, route to
        # get_report (which fetches full markdown) instead of
        # list_reports (which only returns metadata).
        if intent == Intent.REPORT and slots and "report_id" in slots:
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
    def _build_args(args_schema: type, symbol: str, slots: dict | None = None) -> Any:
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
            for k in ("limit", "include_disabled"):
                if k in slots:
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
