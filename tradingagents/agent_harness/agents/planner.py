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
            content = self._llm_complete(prompt, temperature=0.0)
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
