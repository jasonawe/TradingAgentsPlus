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


def describe_impact(
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
    user_message: str | None = None,
) -> str:
    """生成写操作的 impact 描述(给前端 confirm dialog 用)。

    Args:
        tool_name: 工具名
        tool_args: 工具参数(用于摘要展示)
        user_message: 用户原始语义(用于生成更友好的描述)

    Returns:
        人类可读的描述字符串
    """
    base = WRITE_TOOL_IMPACT.get(
        tool_name,
        WRITE_TOOL_IMPACT.get(
            tool_name.split("_", 1)[-1] if "_" in tool_name else "",
            f"执行 {tool_name} 写擰作",
        ),
    )
    # §Step 11 — prepend a friendly asset-scoped line when the
    # tool args carry a recognisable asset symbol. The UI shows this
    # above the technical ``参数: ...`` line.
    asset = None
    body = None
    if tool_args:
        for k in ("symbol", "ticker"):
            v = tool_args.get(k)
            if v:
                asset = str(v)
                break
        body = tool_args.get("body_md") or tool_args.get("note")
    friendly = base
    if asset:
        if tool_name.startswith("create_note") and body:
            body_str = str(body)
            preview = body_str[:40].replace(chr(10), " ")
            if len(body_str) > 40:
                friendly = f'将为 {asset} 添加一条笔记: “...{preview}...”'
            else:
                friendly = f'将为 {asset} 添加一条笔记: “{body_str}”' 
        elif tool_name.startswith("create_alert"):
            thresh = tool_args.get("threshold")
            direction = tool_args.get("direction") or "above"
            friendly = f"将为 {asset} 设置一条价格告警(超过 {thresh} 时触发)" if thresh                 else f"将为 {asset} 创建一条告警"
        elif tool_name.startswith("add_to_watchlist"):
            friendly = f"将 {asset} 加入关注列表"
        elif tool_name.startswith("delete"):
            friendly = f"将删除 {asset} 的记录"
        else:
            friendly = f"将对 {asset} 执行写操作"
    if tool_args and not asset:
        keys = ", ".join(f"{k}={v}" for k, v in list(tool_args.items())[:3])
        if keys:
            return f"{friendly}。参数:{keys}"
    return friendly


# ════════════════════════════════════════════════════════
# Max tool calls 限制(防 LLM 失控)
# ════════════════════════════════════════════════════════

# §Step 13 — reason_short (banner-length action label)
# Short banner versions for the confirm dialog header. Kept ≤ 14 chars
# so the banner stays one line on mobile widths.
WRITE_TOOL_REASON_SHORT: dict[str, str] = {
    "create_alert": "新建告警",
    "update_alert": "修改告警",
    "delete_alert": "删除告警",
    "create_note": "新增笔记",
    "update_note": "修改笔记",
    "delete_note": "删除笔记",
    "create_scheduled_task": "新建定时任务",
    "update_scheduled_task": "修改定时任务",
    "delete_scheduled_task": "删除定时任务",
    "update_preference": "修改偏好",
}


def describe_reason_short(tool_name: str) -> str:
    """§Step 13 — banner-length action label for confirm dialog.

    Returns a short Chinese phrase (≤ 6 chars) suitable for the dialog
    header. Falls back to a constructed verb-form name when the tool is
    unknown so we never return an empty string.
    """
    return WRITE_TOOL_REASON_SHORT.get(
        tool_name,
        WRITE_TOOL_REASON_SHORT.get(
            tool_name.split("_", 1)[-1] if "_" in tool_name else "",
            f"执行 {tool_name}",
        ),
    )


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
    "describe_reason_short",
    "check_max_tool_calls",
    "build_confirmation",
    "validate_write_intent",
]

def validate_write_intent(
    tool_name: str, tool_args: dict[str, Any] | None = None
) -> tuple[bool, str, str]:
    """一体化校验写操作意图(O6v2 HITL 入口)。

    Args:
        tool_name: 工具名
        tool_args: 工具参数

    Returns:
        (is_write, impact, reason_short):
            is_write: True 表示需要 confirm(HITL)
            impact: 人类可读的影响描述(给前端 dialog 用)
            reason_short: §Step 13 — banner-length action label
    """
    is_write = is_write_tool(tool_name)
    impact = describe_impact(tool_name, tool_args or {})
    reason_short = describe_reason_short(tool_name)
    return is_write, impact, reason_short


# 更新 __all__

