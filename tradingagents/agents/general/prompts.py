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

def _format_tool_descriptions(tools: Iterable[Any]) -> str:
    """把 LangChain tools 转成 - tool_name: description 列表。"""
    lines = []
    for t in tools:
        name = getattr(t, "name", str(t))
        desc = getattr(t, "description", "") or ""
        # 截断到 200 字符避免 prompt 过长
        if len(desc) > 200:
            desc = desc[:197] + "..."
        lines.append(f"- `{name}`: {desc}")
    return "\n".join(lines) if lines else "(无可用工具)"


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
        tool_descriptions=_format_tool_descriptions(tools),
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
