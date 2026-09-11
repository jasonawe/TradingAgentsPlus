"""Stage C Agent Prompt 模板 — 借鉴 WrenAI guided/direct 双模式。

- SYSTEM_PROMPT_BASE  : system prompt 主模板(中文,4 占位符)
- render_system_prompt : 把 tools / preferences / mode 拼好
- ROUTING_PROMPT       : 简单 intent classification(查询 / 操作 / 分析)
- render_routing_prompt: 给 routing LLM 用的 prompt
- CONFIRMATION_PROMPT  : 写操作确认模板(O6v2 HITL)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable


# ════════════════════════════════════════════════════════
# System Prompt 模板
# ════════════════════════════════════════════════════════

SYSTEM_PROMPT_BASE = """你是 TradingAgents 理财通用 Agent,帮助用户做投资决策辅助。

## 你的工具能力
{tool_descriptions}

## 当前用户偏好
{user_preferences}

## 当前日期
{current_date}

## 强制 Tool Calling 准则(关键)
6. **涉及具体数据必须先调 tool**。如果用户问"现在多少钱 / 涨了多少 / 估值多少 / 什么新闻",
   **必须** 先调用对应工具(get_quote / get_quotes_batch / get_fundamentals / get_news / get_history),
   **不能** 凭印象 / 训练知识直接编造数字。
7. **首次对话不输出自我介绍**。直接根据用户问题决定调什么 tool。
8. **找不到合适 tool 时简短说明**,不要重复输出能力清单。

## 行为准则
1. **默认只读**。写操作(创建告警 / 笔记 / 调度任务)需要用户显式确认(HITL)。
2. **数据 stale 透明**。涉及具体数据时,主动说明 `fetched_at` 时间。
3. **不确定就问**。拿不准的标的代码 / 业务术语,先问用户或用工具查。
4. **引用偏好**。判断与用户历史偏好相关时,简短说明依据。
5. **推理简短**。直接给结论 + 证据,不要冗长展开。

## 输出格式
- 中文为主,英文术语保留(ticker / PE / ROE 等)
- 涉及具体数据给出**数据来源**(哪个 tool / 哪个 provider)
- 引用历史对话 / 偏好要明确标注(用「根据你之前的...」「按你的偏好...」)
"""


# ════════════════════════════════════════════════════════
# 渲染函数
# ════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════
# Day 10: Tool Priority — 高频 tool 优先展示给 LLM
# (低频 tool 描述被截断时,高频 tool 还能完整保留)
# ════════════════════════════════════════════════════════

# 数值越大越靠前。优先级 > 0 的 tool 永远完整展示。
_TOOL_PRIORITY: dict[str, int] = {
    # Tier 1:核心数据查询(LLM 最常用)
    "get_quote": 100,
    "get_quotes_batch": 95,
    "get_history": 90,
    "get_fundamentals": 85,
    # Tier 2:主动分析入口(对话升级时必调)
    "run_trading_agents_analysis": 80,
    "get_analysis_status": 75,
    "list_reports": 70,
    "get_news": 65,
    # Tier 3:量化因子
    "list_alpha_factors": 60,
    "compute_alpha_factors": 55,
    "evaluate_alpha": 50,
    # Tier 4:写操作(HITL,需要用户确认)
    "create_note": 40,
    "create_alert": 40,
    "update_note": 35,
    "update_alert": 35,
    "delete_note": 30,
    "delete_alert": 30,
    "update_preference": 30,
    # Tier 5:辅助
    "list_watchlist": 25,
    "list_scheduled_tasks": 20,
    "run_scheduled_task": 18,
}


def _sort_tools_by_priority(tools: Iterable[Any]) -> list[Any]:
    """按 _TOOL_PRIORITY 降序排,未列出的放最后(保持原顺序)。"""
    tools_list = list(tools)

    def _key(t: Any) -> tuple[int, int]:
        name = getattr(t, "name", str(t))
        return (-_TOOL_PRIORITY.get(name, 0), tools_list.index(t))

    return sorted(tools_list, key=_key)


def _format_tool_descriptions(
    tools: Iterable[Any],
    *,
    max_per_tool_chars: int = 150,
    max_total_chars: int = 1500,
) -> str:
    """把 LangChain tools 转成 - tool_name: description 列表。

    Args:
        tools: LangChain tool 列表
        max_per_tool_chars: 单个 tool description 最大字符数(默认 150)
        max_total_chars: 所有 tool descriptions 总字符数上限(默认 1500)

    Returns:
        markdown bullet list,超出总上限时追加"(还有 N 个工具...)"标记
    """
    # Day 10: 先按 priority 排序(高频 tool 优先展示给 LLM)
    sorted_tools = _sort_tools_by_priority(tools)
    lines = []
    for t in sorted_tools:
        name = getattr(t, "name", str(t))
        desc = getattr(t, "description", "") or ""
        if len(desc) > max_per_tool_chars:
            desc = desc[: max_per_tool_chars - 3] + "..."
        lines.append(f"- `{name}`: {desc}")

    text = "\n".join(lines) if lines else "(无可用工具)"

    # 总长度截断:超出时取前 N 个工具
    if len(text) > max_total_chars:
        kept: list[str] = []
        total = 0
        truncated = 0
        for line in lines:
            if total + len(line) + 1 > max_total_chars:
                truncated += 1
                continue
            kept.append(line)
            total += len(line) + 1
        if truncated:
            kept.append(f"\n_(还有 {truncated} 个工具未列出,详见工具列表)_")
        text = "\n".join(kept)

    return text


def _format_preferences(preferences: dict[str, Any]) -> str:
    """把 preferences dict 转成 markdown bullet list。"""
    if not preferences:
        return "(未设置)"
    lines = []
    for k, v in sorted(preferences.items()):
        # value 可能是 dict / list / str,简单 to-string
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines)


def render_system_prompt(
    *,
    tools: Iterable[Any],
    preferences: dict[str, Any] | None = None,
    mode: str = "guided",
    current_date: str | None = None,
) -> str:
    """渲染 system prompt。

    Args:
        tools: LangChain tool 列表(或任何带 name/description 属性的对象)
        preferences: L2 用户偏好 dict
        mode: "guided"(强约束,适合弱模型)| "direct"(简洁,适合强模型)
        current_date: 当前日期字符串,默认今天

    Returns:
        完整 system prompt 字符串
    """
    if mode not in ("guided", "direct"):
        mode = "guided"

    base = SYSTEM_PROMPT_BASE.format(
        tool_descriptions=_format_tool_descriptions(
            tools,
            max_per_tool_chars=150,
            max_total_chars=1500,
        ),
        user_preferences=_format_preferences(preferences or {}),
        current_date=current_date or datetime.utcnow().strftime("%Y-%m-%d"),
    )

    if mode == "direct":
        # 强模型用 direct: 加一句"按需调用,不需要调用时直接给答案"
        return base + "\n\n## Direct Mode\n你可以直接给答案,无需调用工具除非必要。\n"

    # guided 模式: 默认就是 base,行为准则已写明
    return base


# ════════════════════════════════════════════════════════
# Routing Prompt(意图分类)
# ════════════════════════════════════════════════════════

ROUTING_PROMPT = """判断用户当前消息的意图属于以下哪一类:

1. **query** — 查询类(quote / history / fundamentals / 因子 / watchlist)
   → 调对应读 tool,直接返回数据
2. **action** — 操作类(创建告警 / 笔记 / 调度任务 / 更新设置)
   → 写 tool + HITL 确认对话框
3. **analysis** — 分析类(深度分析 / 对比 / 量化筛选 / 投资决策)
   → 路由到 TradingAgents 主图(走完整 pipeline)
4. **chat** — 闲聊类(问候 / 解释概念 / 不需要工具的对话)
   → 直接回答,不调工具

用户消息: {user_message}

最近对话历史:
{history}

只返回一个 JSON,不要解释:
{{"intent": "<query|action|analysis|chat>", "confidence": <0.0-1.0>, "reason": "<一句话原因>"}}
"""


def render_routing_prompt(user_message: str, history: list[str] | None = None) -> str:
    """给 routing LLM 用的 prompt(简单 intent classification)。"""
    history_str = "\n".join(history[-5:]) if history else "(无历史)"
    return ROUTING_PROMPT.format(user_message=user_message, history=history_str)


# ════════════════════════════════════════════════════════
# Confirmation Prompt(O6v2 HITL 写操作确认)
# ════════════════════════════════════════════════════════

CONFIRMATION_PROMPT = """即将执行写操作:

**工具**: `{tool_name}`
**参数**: {tool_args}
**影响**: {impact_description}

**审计记录**: 本次操作将写入 `write_audit_log`(actor=agent, status=pending → confirmed → executed)。

确认执行吗?(yes / no)
"""


def render_confirmation_prompt(
    tool_name: str, tool_args: dict[str, Any], impact_description: str
) -> str:
    """渲染写操作确认 prompt(给前端 dialog 展示用)。"""
    import json
    args_str = json.dumps(tool_args, ensure_ascii=False, indent=2)
    return CONFIRMATION_PROMPT.format(
        tool_name=tool_name,
        tool_args=args_str,
        impact_description=impact_description,
    )


# ════════════════════════════════════════════════════════
# Impact 描述生成(给 confirmation prompt 用)
# ════════════════════════════════════════════════════════

WRITE_TOOL_IMPACT = {
    "create_alert": "创建一个新的价格/量化告警,触发时会通过 Webhook(飞书)通知",
    "update_alert": "修改现有告警的启用状态 / 阈值 / 通知方式",
    "delete_alert": "删除现有告警(不可恢复)",
    "create_note": "为某个资产创建笔记,后续可检索 / 编辑",
    "update_note": "修改现有笔记内容",
    "delete_note": "删除笔记(软删除,标记 deleted_at)",
    "create_scheduled_task": "创建定时分析任务,按 cron 表达式自动跑",
    "update_scheduled_task": "修改定时任务的 cron / 参数 / 启用状态",
    "delete_scheduled_task": "删除定时任务",
    "update_preference": "修改用户偏好(影响后续 agent 行为)",
}


def impact_for(tool_name: str, tool_args: dict[str, Any] | None = None) -> str:
    """生成写操作的 impact 描述。"""
    base = WRITE_TOOL_IMPACT.get(tool_name, f"执行 {tool_name} 写操作")
    if tool_args:
        # 简短摘要 args
        keys = ", ".join(f"{k}={v}" for k, v in list(tool_args.items())[:3])
        if keys:
            return f"{base}。参数:{keys}"
    return base


__all__ = [
    "SYSTEM_PROMPT_BASE",
    "render_system_prompt",
    "ROUTING_PROMPT",
    "render_routing_prompt",
    "CONFIRMATION_PROMPT",
    "render_confirmation_prompt",
    "WRITE_TOOL_IMPACT",
    "impact_for",
]
