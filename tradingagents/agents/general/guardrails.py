"""Stage C Agent 操作治理 — O6v2 写操作拦截 + max_tool_calls 限制。

借鉴 WrenAI policy.py 的"永远只读 + strict mode"三层防线,
但简化:
- 写操作工具黑名单(WARN_TOOLS / BLOCK_TOOLS)
- max_tool_calls 限制(防 LLM 失控)
- confirmation prompt 生成(给前端 dialog)
"""

from __future__ import annotations

import fnmatch
from typing import Any


# ════════════════════════════════════════════════════════
# 写操作工具黑名单
# ════════════════════════════════════════════════════════

WRITE_TOOLS: set[str] = {
    # 告警
    "create_alert", "update_alert", "delete_alert",
    # 笔记
    "create_note", "update_note", "delete_note",
    # 定时任务
    "create_scheduled_task", "update_scheduled_task", "delete_scheduled_task",
    # 偏好
    "update_preference",
}


WRITE_TOOL_PATTERNS: tuple[str, ...] = (
    "create_*", "update_*", "delete_*",
)


# ════════════════════════════════════════════════════════
# 写操作判断
# ════════════════════════════════════════════════════════

def is_write_tool(tool_name: str) -> bool:
    """判断是否为写操作(精确匹配 + fnmatch 模式)。

    Args:
        tool_name: 工具名

    Returns:
        True 如果是写操作
    """
    if tool_name in WRITE_TOOLS:
        return True
    for pattern in WRITE_TOOL_PATTERNS:
        if fnmatch.fnmatch(tool_name, pattern):
            return True
    return False


# ════════════════════════════════════════════════════════
# 写操作影响描述(O6v2 confirm 展示用)
# ════════════════════════════════════════════════════════

WRITE_TOOL_IMPACT: dict[str, str] = {
    "create_alert": "创建价格/量化告警,触发时通过 Webhook 通知",
    "update_alert": "修改告警的启用状态 / 阈值 / 通知方式",
    "delete_alert": "删除告警(不可恢复)",
    "create_note": "为某个资产创建笔记",
    "update_note": "修改现有笔记内容",
    "delete_note": "删除笔记(软删除)",
    "create_scheduled_task": "创建定时分析任务(按 cron 自动跑)",
    "update_scheduled_task": "修改定时任务的 cron / 参数",
    "delete_scheduled_task": "删除定时任务",
    "update_preference": "修改用户偏好(影响后续 agent 行为)",
}


def describe_impact(tool_name: str, tool_args: dict[str, Any] | None = None) -> str:
    """生成写操作的 impact 描述(给前端 confirm dialog 用)。

    Args:
        tool_name: 工具名
        tool_args: 工具参数(用于摘要展示)

    Returns:
        人类可读的描述字符串
    """
    base = WRITE_TOOL_IMPACT.get(
        tool_name,
        WRITE_TOOL_IMPACT.get(
            tool_name.split("_", 1)[-1] if "_" in tool_name else "",
            f"执行 {tool_name} 写操作",
        ),
    )
    if tool_args:
        keys = ", ".join(f"{k}={v}" for k, v in list(tool_args.items())[:3])
        if keys:
            return f"{base}。参数:{keys}"
    return base


# ════════════════════════════════════════════════════════
# Max tool calls 限制(防 LLM 失控)
# ════════════════════════════════════════════════════════

def check_max_tool_calls(call_count: int, max_calls: int = 10) -> bool:
    """检查是否超过 max_tool_calls 上限。

    Args:
        call_count: 当前累计 tool 调用次数
        max_calls: 最大允许次数(默认 10)

    Returns:
        True 表示还可以继续;False 表示已达上限
    """
    return call_count < max_calls


# ════════════════════════════════════════════════════════
# Confirmation prompt(O6v2 完整版,跟 prompts.py 的简化版互补)
# ════════════════════════════════════════════════════════

def build_confirmation(
    tool_name: str,
    tool_args: dict[str, Any],
    user_message: str | None = None,
) -> dict[str, Any]:
    """构造确认对话框 payload(给前端用)。

    Returns:
        {
            "tool_name": str,
            "tool_args": dict,
            "impact": str,
            "user_message": str?,
            "audit_status": "pending"
        }
    """
    return {
        "tool_name": tool_name,
        "tool_args": tool_args,
        "impact": describe_impact(tool_name, tool_args),
        "user_message": user_message,
        "audit_status": "pending",
    }


__all__ = [
    "WRITE_TOOLS",
    "WRITE_TOOL_PATTERNS",
    "WRITE_TOOL_IMPACT",
    "is_write_tool",
    "describe_impact",
    "check_max_tool_calls",
    "build_confirmation",
]
