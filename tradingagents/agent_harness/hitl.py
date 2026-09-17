"""Stage C — 写操作 approval registry (in-memory, per-session)。

HITL 流程:
1. LLM 调用 write tool → tool 看到没有 approval → 返回 "AWAITING_CONFIRMATION" + 结构化 payload
2. Orchestrator 收到 → 写 audit log (status=pending) → 发 confirm_request event → 停
3. 用户在 Drawer 点 确认/拒绝 → 前端 POST /confirm
4. 后端:
   - 更新 audit log
   - 若 approved → 把 (session_id, tool_name, args_json) 写入 _approved 集合
   - 重新调用 agent,LLM 再决定调同一个 tool,这次 tool 看到 approval → 真正执行

简化设计:
- 单次 approval,执行后立刻移除(set 行为)
- key = (session_id, tool_name, frozenset(args.items())) 用来识别同一个调用
- 跨进程需要持久化时,可以写 SQLite(暂不实现)
"""

from __future__ import annotations

import json
import threading
from typing import Any


_lock = threading.RLock()

# session_id -> set of (tool_name, args_json)
_approved: dict[str, set[tuple[str, str]]] = {}


def _key_of(tool_name: str, tool_args: dict[str, Any]) -> tuple[str, str]:
    """生成稳定的 approval key(deterministic JSON 序列化)。"""
    return (tool_name, json.dumps(tool_args, ensure_ascii=False, sort_keys=True))


def is_approved(session_id: str, tool_name: str, tool_args: dict[str, Any]) -> bool:
    """检查某次调用是否已被用户批准。"""
    with _lock:
        s = _approved.get(session_id)
        if not s:
            return False
        return _key_of(tool_name, tool_args) in s


def grant_approval(session_id: str, tool_name: str, tool_args: dict[str, Any]) -> None:
    """批准一次调用(单次有效,执行后由 consume_approval 清除)。"""
    with _lock:
        s = _approved.setdefault(session_id, set())
        s.add(_key_of(tool_name, tool_args))


def consume_approval(session_id: str, tool_name: str, tool_args: dict[str, Any]) -> None:
    """消费一次 approval(执行成功后调用,避免重复执行)。"""
    with _lock:
        s = _approved.get(session_id)
        if not s:
            return
        s.discard(_key_of(tool_name, tool_args))


def revoke_session(session_id: str) -> None:
    """清除 session 的所有 approval(测试 / 强制 reset)。"""
    with _lock:
        _approved.pop(session_id, None)


def list_pending(session_id: str) -> list[dict[str, Any]]:
    """列出当前 session 已批准但未消费的(调试用)。"""
    with _lock:
        s = _approved.get(session_id, set())
        out = []
        for tool_name, args_json in s:
            try:
                args = json.loads(args_json)
            except json.JSONDecodeError:
                args = {}
            out.append({"tool_name": tool_name, "tool_args": args})
        return out


__all__ = [
    "is_approved",
    "grant_approval",
    "consume_approval",
    "revoke_session",
    "list_pending",
]
