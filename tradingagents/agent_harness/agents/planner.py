"""PlannerAgent — JSON plan generation (v3 spec §4.1).

⚠️  DEPRECATED (2026-09-16): The main orchestrator path uses
``Orchestrator._llm_plan`` directly (see
``tradingagents/agent_harness/core/orchestrator.py``), NOT this
``PlannerAgent``. This class stays in the agent registry for
subagent-provider entry-point compatibility but is no longer on the
critical path. The CRUD dispatch table
(``Orchestrator._CRUD_DISPATCH``) handles write intents
deterministically BEFORE the LLM plan runs.

This module is kept so:
- the heuristic plan for write intents can be reused by tests
- entry-point registration via ``agent_harness.subagents`` continues
  to work
- the catalogue of write tools (``_WRITE_TOOL_CATALOG``) is
  discoverable from one place for any future agent that wants it

LLM-backed in production; falls back to heuristic plan when no LLM is
wired so the agent remains testable without API keys.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .base import AgentContext, AgentInput, AgentResult, BaseAgent

LOGGER = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"\b[A-Z0-9]{1,6}(?:\.[A-Z]{2})?\b")

_WRITE_TOOL_CATALOG = (
    # Tools the planner can dispatch via ``action=<name>`` directly. The
    # orchestrator's HITL gate intercepts these so the user gets a confirm
    # dialog. Descriptions are short and intent-oriented so the LLM
    # matches user Chinese / English phrasing without ambiguity.
    "create_note (symbol, body_md) — append a research note to a symbol",
    "update_note (note_id, body_md) — overwrite an existing note body",
    "delete_note (note_id) — soft-delete one note",
    "delete_notes_for_symbol (symbol) — bulk-delete every note for a symbol",
    "create_alert (symbol, kind, params) — add a price / volume alert",
    "update_alert (alert_id, params) — change alert threshold or cooldowns",
    "delete_alert (alert_id) — remove one alert",
    "delete_alerts_for_symbol (symbol) — bulk-delete every alert for a symbol",
    "add_to_watchlist (symbol, note?) — add symbol to user watchlist",
    "remove_from_watchlist (symbol) — remove symbol from user watchlist",
    "create_scheduled_task (symbol, cron, model, analysis_type) — schedule analysis",
    "update_scheduled_task / delete_scheduled_task / run_scheduled_task",
    "delete_scheduled_tasks_for_symbol (symbol) — bulk-delete scheduled tasks for a symbol",
)

_PLAN_SYSTEM = (
    "You are a finance research planner. Reply ONLY with valid JSON. "
    "No commentary, no markdown fences. Output schema: "
    "[{\"step\": <int>, \"agent\": OR \"action\": <name>, \"args\": {<dict>}}]\n"
    "Rules:\n"
    "- Use \"action\":<tool_name> for HITL-gated write tools (any tool that "
    "creates / updates / deletes persistent state). The orchestrator auto-"
    "prompts the user for approval. NEVER pretend to have created / deleted "
    "something in your plan — if the user asked for it, emit the matching "
    "write tool call.\n"
    "- Use \"agent\":<agent_name> for read-only research agents "
    "(data_agent / alpha_agent / news_agent). The agent picks the tool.\n"
    "- Add a final synthesizer step when the answer needs synthesis."
)


class PlannerAgent(BaseAgent):
    name = "planner"
    description = "Generate a JSON plan from user message + agent capabilities."
    tools: list = []
    system_prompt = _PLAN_SYSTEM

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        symbols = _TICKER_RE.findall(input.user_message or "")

        # LLM path: ask the model for a structured JSON plan.
        if self._llm_available():
            caps_lines = []
            for cap in self._agent_caps():
                # Each entry is either {"agent": ..., "capability": ...} (read agent)
                # or {"tool": ..., "capability": ...} (HITL-gated write tool).
                # Render uniformly so the LLM sees both kinds.
                key = cap.get("agent") or cap.get("tool") or "?"
                caps_lines.append(f"- {key}: {cap['capability']}")
            prompt = (
                f"User message: {input.user_message}\n\n"
                f"Detected symbols: {symbols}\n\n"
                "Available agents and tools:\n" + "\n".join(caps_lines) +
                "\n\nGenerate a JSON plan as a list of {step, agent OR action, args} objects."
            )
            content = self._llm_complete(prompt, temperature=0.0, mode="deep")
            plan = self._parse_plan(content) if content else []
            if plan:
                return AgentResult(
                    success=True,
                    content=json.dumps(plan, ensure_ascii=False),
                    structured_data={"plan": plan, "symbols": symbols, "source": "llm"},
                )
            LOGGER.debug("PlannerAgent: LLM plan unusable, falling back to heuristic")

        # Heuristic fallback.
        plan = self._heuristic_plan(input.user_message, symbols)
        return AgentResult(
            success=True,
            content=json.dumps(plan, ensure_ascii=False),
            structured_data={"plan": plan, "symbols": symbols, "source": "heuristic"},
        )

    @staticmethod
    def _heuristic_plan(message: str, symbols: list[str]) -> list[dict[str, Any]]:
        if not symbols:
            return [{"step": 1, "agent": "synthesizer", "args": {"ask_user_for_symbol": True}}]

        # §7.3 #11 — heuristic fallback MUST recognise write-intents so a
        # no-LLM / LLM-broken session still emits the right HITL tool
        # instead of synthesising a fake "已添加" reply.
        msg = message or ""
        sym0 = symbols[0]

        # 1) 加笔记:支持中英文 + 全/半角冒号 + body 在冒号后或句末
        body = None
        for kw in ("笔记:", "笔记:", "note:", "Note:", "note is"):
            if kw in msg:
                body = msg.split(kw, 1)[1].strip()
                break
        if body is None and ("笔记" in msg or "note" in msg.lower()):
            # 末尾 " : <body>" 或 " \u2014 <body>" 的兜底
            m = re.search(r"[:：]\s*(.+)$", msg)
            if m:
                body = m.group(1).strip()
        is_add_note = any(kw in msg.lower() for kw in (
            "加笔记", "加一条笔记", "加一个笔记", "新增笔记", "写笔记",
            "add a note", "add note", "append note", "create note",
        ))
        if is_add_note and body:
            return [{"step": 1, "action": "create_note", "args": {"symbol": sym0, "body_md": body}}]

        # 2) 加告警
        is_add_alert = any(kw in msg for kw in ("加告警", "新建告警", "加一个告警", "add alert", "create alert"))
        if is_add_alert and sym0:
            return [{"step": 1, "action": "create_alert", "args": {"symbol": sym0, "kind": "price", "params": {}}}]

        # 3) 加关注
        is_add_watch = any(kw in msg for kw in ("加入关注", "加入我的关注", "加关注", "关注一下", "add to watchlist"))
        if is_add_watch and sym0:
            return [{"step": 1, "action": "add_to_watchlist", "args": {"symbol": sym0}}]

        # 4) 删除全部笔记 / 全部告警 / 全部定时任务 — 顺序不固定
        msg_l = msg  # case-insensitive compare target
        if sym0 and any(kw in msg for kw in (
            "笔记都删", "笔记删了", "删了笔记", "删除笔记", "全部笔记",
            "的所有笔记", "所有笔记都删", "把笔记",
            "delete notes", "delete all notes", "remove notes",
        )):
            return [{"step": 1, "action": "delete_notes_for_symbol", "args": {"symbol": sym0}}]
        if sym0 and any(kw in msg for kw in (
            "告警都删", "告警删了", "删了告警", "删除告警",
            "delete alerts", "delete all alerts",
        )):
            return [{"step": 1, "action": "delete_alerts_for_symbol", "args": {"symbol": sym0}}]
        if sym0 and any(kw in msg for kw in (
            "定时都删", "定时删了", "删了定时", "删除定时", "任务都删",
            "delete scheduled", "delete all scheduled",
        )):
            return [{"step": 1, "action": "delete_scheduled_tasks_for_symbol", "args": {"symbol": sym0}}]

        # default: read flow
        plan: list[dict[str, Any]] = []
        for idx, sym in enumerate(symbols, start=1):
            plan.append({"step": idx, "agent": "data_agent", "args": {"symbol": sym}})
        plan.append({"step": len(plan) + 1, "agent": "synthesizer", "args": {}})
        return plan

    @staticmethod
    def _parse_plan(content: str | None) -> list[dict[str, Any]]:
        if not content:
            return []
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except Exception:
            LOGGER.warning("PlannerAgent: LLM plan returned non-JSON: %s", content[:120])
        return []

    def _agent_caps(self) -> list[dict[str, Any]]:
        """Read capabilities from the agent_registry if we can find one.

        The registry isn't injected by default; subclasses (or wiring code)
        can stash a reference via ``self.tool_registry._agent_registry`` if
        they want richer prompts. We keep the fallback short.
        """
        reg = getattr(self, "_agent_registry", None)
        agents = (
            reg.plan_capabilities() if reg is not None else [
                {"agent": "data_agent", "capability": "quote + fundamentals"},
                {"agent": "alpha_agent", "capability": "alpha158 factor compute + IC"},
                {"agent": "news_agent", "capability": "news + sentiment"},
                {"agent": "synthesizer", "capability": "synthesize final answer"},
            ]
        )
        # Append HITL-gated write tools so the LLM knows they exist (otherwise
        # it would fabricate '已添加' without dispatching any tool call).
        write_tools = [
            {"tool": line.split(" (")[0].strip(), "capability": line}
            for line in _WRITE_TOOL_CATALOG
        ]
        return agents + write_tools


# ════════════════════════════════════════════════════════
# V2 — PlanGraph output (Task 15)
# ════════════════════════════════════════════════════════

# Plan-local task_key namespaces — kept short and stable for testability
_DATA_TASK_PREFIX = "data"
_NEWS_TASK_PREFIX = "news"
_ALPHA_TASK_PREFIX = "alpha"

# Rejected agent / action names — Planner 不应发出
_REJECTED_AGENTS = frozenset({"verifier", "synthesizer"})
_REJECTED_ACTIONS = frozenset({
    "create_note", "update_note", "delete_note", "delete_notes_for_symbol",
    "create_alert", "update_alert", "delete_alert", "delete_alerts_for_symbol",
    "add_to_watchlist", "remove_from_watchlist",
    "create_scheduled_task", "update_scheduled_task",
    "delete_scheduled_task", "delete_scheduled_tasks_for_symbol",
    "run_scheduled_task",
})


def _intent_from_message(msg: str) -> set[str]:
    """Classify the user's intent by keyword presence."""
    m = msg or ""
    out = set()
    if any(kw in m for kw in ("新闻", "news", "资讯")):
        out.add("news")
    if any(kw in m for kw in ("alpha", "compute", "因子", "IC")):
        out.add("alpha")
    # default: data
    out.add("data")
    return out


class _PlanCache:
    """Tiny LRU cache keyed by (user_message, tuple(symbols))."""

    def __init__(self, max_entries: int = 256) -> None:
        self._entries: dict[tuple, Any] = {}
        self._max = max_entries

    def _key(self, user_message: str, symbols: list[str]) -> tuple:
        return (user_message.strip(), tuple(symbols or []))

    def get(self, user_message: str, symbols: list[str]):
        return self._entries.get(self._key(user_message, symbols))

    def put(self, user_message: str, symbols: list[str], graph) -> None:
        if len(self._entries) >= self._max:
            # evict oldest arbitrary entry
            self._entries.pop(next(iter(self._entries)))
        self._entries[self._key(user_message, symbols)] = graph


def _v2_plan_via_llm(planner, message: str, symbols: list[str]) -> list[dict]:
    """Try to get a V2 plan from the LLM.

    Returns ``[]`` when LLM is unavailable, returns invalid JSON, or
    returns a plan that contains rejected agents / write actions.
    """
    if not planner._llm_available():
        return []
    caps = planner._capability_catalog()
    caps_lines = [
        f"- {c['agent']}: {c['capability']}" for c in caps
    ]
    prompt = (
        f"User message: {message}\n\n"
        f"Symbols: {symbols}\n\n"
        "Available agents (domain only — no verifier/synthesizer/tools):\n"
        + "\n".join(caps_lines)
        + '\n\nReturn a JSON list of {"task_key": str, "agent": str, '
        '"capability": str, "objective": str, "inputs": dict, '
        '"required": bool}. task_key is plan-local (e.g. "data_aapl").'
    )
    try:
        content = planner._llm_complete(prompt, temperature=0.0, mode="deep")
    except Exception:
        return []
    if not content:
        return []
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    cleaned: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        agent = entry.get("agent") or entry.get("action") or ""
        if agent in _REJECTED_AGENTS or agent in _REJECTED_ACTIONS:
            # 拒绝 verifier / synthesizer / 写 tool
            continue
        cleaned.append(entry)
    return cleaned


def _v2_plan_heuristic(symbols: list[str], intents: set[str]) -> list[dict]:
    """Deterministic fallback: emit one task per (symbol × intent)."""
    if not symbols:
        return []
    plan: list[dict] = []
    for sym in symbols:
        for intent in sorted(intents):
            if intent == "data":
                plan.append({
                    "task_key": f"{_DATA_TASK_PREFIX}_{sym.lower()}",
                    "agent": "data_agent",
                    "capability": "domain_lookup",
                    "objective": f"lookup quote + fundamentals for {sym}",
                    "inputs": {"symbol": sym, "carry_symbols": []},
                    "required": True,
                })
            elif intent == "news":
                plan.append({
                    "task_key": f"{_NEWS_TASK_PREFIX}_{sym.lower()}",
                    "agent": "news_agent",
                    "capability": "news_lookup",
                    "objective": f"fetch recent news for {sym}",
                    "inputs": {"symbol": sym},
                    "required": False,
                })
            elif intent == "alpha":
                plan.append({
                    "task_key": f"{_ALPHA_TASK_PREFIX}_{sym.lower()}",
                    "agent": "alpha_agent",
                    "capability": "alpha_compute",
                    "objective": f"compute alpha factors for {sym}",
                    "inputs": {"symbol": sym},
                    "required": False,
                })
    return plan


# Monkey-patch: extend PlannerAgent with V2 method
def _planner_plan_v2(self, input: AgentInput, *, context: AgentContext):
    """V2 entry point — return a typed ``PlanGraph`` (domain tasks only)."""
    from tradingagents.agent_harness.runtime.models import (
        PlanGraph as _PlanGraph,
        PlanTask as _PlanTask,
        RunBudgets as _RunBudgets,
    )

    symbols = _TICKER_RE.findall(input.user_message or "")
    extra_symbols = (input.context or {}).get("symbols") or []
    if isinstance(extra_symbols, list):
        symbols = list(dict.fromkeys(list(symbols) + list(extra_symbols)))

    # Plan cache hit
    if self._plan_cache is None:
        self._plan_cache = _PlanCache()
    cached = self._plan_cache.get(input.user_message, symbols)
    if cached is not None:
        return cached

    # LLM 路径(可能 fallback)
    raw = _v2_plan_via_llm(self, input.user_message, symbols)
    if not raw:
        intents = _intent_from_message(input.user_message)
        raw = _v2_plan_heuristic(symbols, intents)

    # 转换为 typed PlanTask
    domain_tasks = []
    for entry in raw:
        try:
            task = _PlanTask(
                task_key=str(entry["task_key"]),
                agent=str(entry["agent"]),
                capability=str(entry.get("capability") or ""),
                objective=str(entry.get("objective") or ""),
                inputs=dict(entry.get("inputs") or {}),
                depends_on=list(entry.get("depends_on") or []),
                required=bool(entry.get("required", True)),
            )
        except Exception:
            continue
        domain_tasks.append(task)

    budgets = _RunBudgets(
        max_dynamic_tasks=6,
        max_messages=20,
        max_handoff_depth=2,
        max_repairs_per_task=1,
        max_total_tokens=4000,
        deadline_at=_deadline_default(),
    )
    graph = _PlanGraph(domain_tasks=domain_tasks, budgets=budgets)
    self._plan_cache.put(input.user_message, symbols, graph)
    return graph


def _deadline_default():
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat()


PlannerAgent.plan_v2 = _planner_plan_v2  # type: ignore[attr-defined]


def _planner_capability_catalog(self) -> list[dict[str, Any]]:
    """V2 capability catalog — pulled from the AgentRegistry V2 entries.

    Only domain agents (data / news / alpha) are exposed; verifier and
    synthesizer are excluded — they are runtime-added.
    """
    reg = getattr(self, "agent_registry", None)
    if reg is None:
        return [
            {"agent": "data_agent", "capability": "domain_lookup"},
            {"agent": "alpha_agent", "capability": "alpha_compute"},
            {"agent": "news_agent", "capability": "news_lookup"},
        ]
    out: list[dict[str, Any]] = []
    for name in reg.list():
        descriptor = reg.descriptor(name)
        if descriptor is None:
            continue
        if name in _REJECTED_AGENTS:
            continue
        for cap in descriptor.capabilities:
            out.append({"agent": name, "capability": cap})
    return out


PlannerAgent._capability_catalog = _planner_capability_catalog  # type: ignore[attr-defined]


# Allow AgentRegistry injection — PlannerAgent.__init__ can accept agent_registry
_orig_init = PlannerAgent.__init__


def _patched_init(self, *, agent_registry=None, **kwargs):
    _orig_init(self, **kwargs)
    self.agent_registry = agent_registry
    self._plan_cache = _PlanCache()


PlannerAgent.__init__ = _patched_init  # type: ignore[assignment]
