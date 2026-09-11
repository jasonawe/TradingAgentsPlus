"""Stage C — 意图路由:关键词加速 + LLM classification fallback。

设计借鉴 OpenBB MCP-first 模式:
- fast_route() 用 regex 覆盖 80% 简单 query,零 LLM 开销
- classify_intent() 失败时 fallback 到 LLM 分类,confidence > 阈值才信任
- orchestrator 在 build_agent / stream_chat 入口调一次,把 Intent 注入 system prompt
  强化 tool 引导(QUERY/ACTION/CHAT)或者直接路由 ANALYSIS

来源: docs/superpowers/specs/2026-09-10-finance-general-agent-design.md
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .prompts import render_routing_prompt

LOGGER = logging.getLogger(__name__)


class Intent(str, Enum):
    """用户意图分类(与 prompts.ROUTING_PROMPT 对齐)。"""
    QUERY = "query"          # 查 quote / history / fundamentals / 因子 / watchlist
    ACTION = "action"        # 建告警 / 笔记 / 调度 / 偏好更新
    ANALYSIS = "analysis"    # 深度分析 / 对比 / 量化筛选 → 路由到主图
    CHAT = "chat"            # 闲聊 / 概念解释 / 不需要工具


# ─────────────────────────────────────────────────────
# 关键词加速 — 命中即返回,不走 LLM
# ─────────────────────────────────────────────────────

# 顺序很重要:从前到后匹配,先识别 CHAT(避免"什么是 PE"被误判为 query)。
# "加仓吗" → analysis; "新建告警" → action; "600036 多少钱" → query。
FAST_ROUTES: list[tuple[re.Pattern[str], Intent]] = [
    # CHAT — 闲聊/概念解释,优先匹配(避免"什么是 PE"误判)
    (re.compile(r"^(你好|hi|hello|hey|嗨|哈喽)[\s!?,.]*$"), Intent.CHAT),
    (re.compile(r"^(什么是|解释|什么叫|为什么|原理|区别)"), Intent.CHAT),
    # ACTION — 明确动作动词
    (re.compile(r"(新建|创建|加个|给我建|帮我建|记一下|建一个|建个).*(告警|提醒|笔记|备忘|调度|任务)"), Intent.ACTION),
    (re.compile(r"(删除|移除|取消).*(告警|笔记|任务|提醒)"), Intent.ACTION),
    (re.compile(r"(更新|修改|设置|改成).*(偏好|设置|配置)"), Intent.ACTION),
    # ANALYSIS — 加仓/减仓/分析/决策类
    (re.compile(r"加仓|减仓|卖出|买入|调仓|值不值得|该不该|要不要|仓位"), Intent.ANALYSIS),
    (re.compile(r"(深度|综合|详细)?(分析|对比|比较|走势|趋势|研判|研报)"), Intent.ANALYSIS),
    (re.compile(r"(风险|回撤|波动率|夏普).*(如何|怎么样|多少|大)"), Intent.ANALYSIS),
    # QUERY — 数据查询(放最后,避免覆盖 chat/analysis)
    (re.compile(r"(行情|股价|价格|多少钱|报价|quote|市盈率|市值|换手|成交|RSI|MACD|ROC|动量|波动|因子|排行|基本面|ROE|盈利|营收)"), Intent.QUERY),
    (re.compile(r"(我的)?(关注|自选|watchlist).*(有什么|列表|全部)?"), Intent.QUERY),
]


def fast_route(user_message: str) -> Intent | None:
    """关键词 regex 匹配;命中返回 Intent,否则 None(让 LLM 来)。"""
    text = user_message.strip()
    if not text:
        return None
    for pattern, intent in FAST_ROUTES:
        if pattern.search(text):
            LOGGER.debug("[routing] fast hit: %s -> %s", text[:30], intent.value)
            return intent
    return None


# ─────────────────────────────────────────────────────
# LLM classification fallback
# ─────────────────────────────────────────────────────

# confidence 阈值:低于此值视为不确定,fallback 到 ANALYSIS (让 LLM ReAct 自己探索)
DEFAULT_CONFIDENCE_THRESHOLD = 0.85


@dataclass
class RouteResult:
    """classify_intent 的返回结果。"""
    intent: Intent
    confidence: float = 1.0
    reason: str = ""
    source: str = "fast"  # "fast" / "llm" / "fallback"


def _parse_llm_intent(content: str) -> tuple[Intent, float, str] | None:
    """LLM 返回的 JSON 解析。"""
    content = content.strip()
    # 提取首个 JSON 块
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        return None
    intent_str = str(data.get("intent", "")).strip().lower()
    try:
        intent = Intent(intent_str)
    except ValueError:
        return None
    conf = float(data.get("confidence", 0.0))
    conf = max(0.0, min(1.0, conf))
    reason = str(data.get("reason", ""))[:200]
    return intent, conf, reason


def _llm_classify(
    user_message: str,
    llm: Any,
    history: list[str] | None,
) -> tuple[Intent, float, str]:
    """调 LLM 做意图分类。失败返回 ANALYSIS(让 ReAct 自由探索)。"""
    prompt = render_routing_prompt(user_message, history)
    try:
        # 支持 langchain BaseChatModel 和 callable
        if hasattr(llm, "invoke"):
            resp = llm.invoke(prompt)
            content = getattr(resp, "content", str(resp))
        elif callable(llm):
            resp = llm(prompt)
            content = getattr(resp, "content", str(resp))
        else:
            return Intent.ANALYSIS, 0.0, "no llm invoke method"
        if isinstance(content, list):
            # 某些 chat model 返回 list of content blocks
            content = "".join(str(c) for c in content)
        parsed = _parse_llm_intent(str(content))
        if parsed is None:
            LOGGER.warning("[routing] LLM response not parseable: %r", content[:200])
            return Intent.ANALYSIS, 0.0, "parse failed"
        return parsed
    except Exception as e:  # noqa: BLE001
        LOGGER.warning("[routing] LLM classify failed: %s", e)
        return Intent.ANALYSIS, 0.0, f"llm error: {e}"


def classify_intent(
    user_message: str,
    *,
    llm: Any | None = None,
    history: list[str] | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> RouteResult:
    """主入口:先 fast_route,失败 fallback LLM,低 confidence 默认 ANALYSIS。

    Args:
        user_message: 用户原始消息
        llm: 可选 chat model(有 .invoke(str) -> object with .content)
        history: 最近对话历史(可选)
        confidence_threshold: LLM 返回 confidence < 此值视为不确定 → fallback

    Returns:
        RouteResult 含 intent / confidence / reason / source
    """
    text = (user_message or "").strip()
    if not text:
        return RouteResult(Intent.CHAT, 1.0, "empty message", "fast")

    # 1) 关键词
    fast = fast_route(text)
    if fast is not None:
        return RouteResult(fast, 1.0, "keyword match", "fast")

    # 2) LLM fallback
    if llm is not None:
        intent, conf, reason = _llm_classify(text, llm, history)
        if conf >= confidence_threshold:
            return RouteResult(intent, conf, reason, "llm")
        LOGGER.info(
            "[routing] LLM confidence %.2f < %.2f, fallback to ANALYSIS",
            conf, confidence_threshold,
        )
        return RouteResult(Intent.ANALYSIS, conf, f"low confidence: {reason}", "fallback")

    # 3) 无 LLM,默认 ANALYSIS(让 ReAct 自由探索)
    return RouteResult(Intent.ANALYSIS, 0.0, "no fast hit + no llm", "fallback")


# ─────────────────────────────────────────────────────
# System prompt 注入片段 — orchestrator 调用
# ─────────────────────────────────────────────────────

INTENT_HINT_TEMPLATE = """## 当前轮意图
LLM 已判断用户当前消息属于 **{intent}**({reason}, confidence={confidence:.2f}, 来源:{source})。

按 {intent} 类意图的行为准则:
{behavior_hint}
"""


def render_intent_hint(result: RouteResult) -> str:
    """生成 system prompt 注入片段,引导 LLM 选对的 tool。"""
    behaviors = {
        Intent.QUERY: "- 优先用读 tool(get_quote / get_history / get_fundamentals / list_alpha_factors 等)拿数据,直接给答案。",
        Intent.ACTION: "- 写 tool(create_note / create_alert / update_preference)需要用户 HITL 确认,准备好 confirm dialog 触发。",
        Intent.ANALYSIS: "- 复杂分析交给 run_trading_agents_analysis(主图),简单判断可先用 alpha factors + quote。",
        Intent.CHAT: "- 直接回答,不调任何 tool。",
    }
    return INTENT_HINT_TEMPLATE.format(
        intent=result.intent.value,
        reason=result.reason or "(无)",
        confidence=result.confidence,
        source=result.source,
        behavior_hint=behaviors[result.intent],
    )


__all__ = [
    "Intent",
    "RouteResult",
    "FAST_ROUTES",
    "fast_route",
    "classify_intent",
    "render_intent_hint",
    "DEFAULT_CONFIDENCE_THRESHOLD",
]
