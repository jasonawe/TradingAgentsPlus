"""Day 3 综合测试:HITL 写工具 + audit log + confirm endpoint。"""
import sys
sys.path.insert(0, '/Users/shenkang/workroom/languages/agent/TradingAgents')

import json

# 1. 模块导入
from tradingagents.agents.general.approval import (
    grant_approval, is_approved, consume_approval, list_pending, revoke_session,
)
from tradingagents.agents.general.audit import (
    log_write, list_writes, update_write_status, VALID_WRITE_STATUSES,
)
from tradingagents.agents.general.guardrails import is_write_tool, validate_write_intent
from tradingagents.agents.general.tools_bridge import (
    ALL_TOOLS, set_repositories, create_note, update_alert, update_preference,
)
from tradingagents.agents.general.orchestrator import _emit_message
from langchain_core.messages import ToolMessage

print("=" * 60)
print("Day 3 综合测试")
print("=" * 60)

# 注入 mock repos
class MockNoteRepo:
    def create(self, symbol, body_md, asset_type="stock"):
        return {"id": f"note-{symbol}", "symbol": symbol, "body_md": body_md}

class MockAlertRepo:
    def create(self, symbol, asset_type, kind, params, cooldown_seconds=3600):
        return {"id": "alert-1", "symbol": symbol, "kind": kind}

set_repositories({"notes": MockNoteRepo(), "alerts": MockAlertRepo()})

# [A] guardrails
print("\n[A] guardrails.is_write_tool:")
assert is_write_tool("create_alert") == True
assert is_write_tool("get_quote") == False
assert is_write_tool("update_alert") == True
print("  ✓ 写工具识别正确")

# [B] tools_bridge HITL gate
print("\n[B] tools_bridge HITL gate:")
# 1) 未批准 → AWAITING_CONFIRMATION
revoke_session("s_t1")
result = create_note.invoke({"session_id": "s_t1", "symbol": "600036.SS", "body_md": "test"})
assert "AWAITING_CONFIRMATION" in result
print("  ✓ 未批准时返回 AWAITING_CONFIRMATION")

# 2) grant + retry → 真实执行
grant_approval("s_t1", "create_note", {"symbol": "600036.SS", "body_md": "test", "asset_type": "stock"})
result2 = create_note.invoke({"session_id": "s_t1", "symbol": "600036.SS", "body_md": "test"})
assert "NOTE_CREATED" in result2
print(f"  ✓ 批准后真实执行: {result2[:60]}...")

# 3) 单次 approval 消费
assert consume_approval("s_t1", "create_note", {"symbol": "600036.SS", "body_md": "test", "asset_type": "stock"}) is None
# 重新调用应该再次 AWAITING_CONFIRMATION
result3 = create_note.invoke({"session_id": "s_t1", "symbol": "600036.SS", "body_md": "test"})
# 这里因为 args 加了 asset_type=stock,所以 key 仍然一样,会再次执行
# 但 consume 后应该清空,所以再调一次需要新 approval
result4 = create_note.invoke({"session_id": "s_t1", "symbol": "600036.SS", "body_md": "different"})
assert "AWAITING_CONFIRMATION" in result4, f"expected AWAIT got: {result4[:80]}"
print("  ✓ 不同 args 不会复用 approval")

# [C] audit log
print("\n[C] audit log:")
audit_id = log_write(tool_name="create_alert", tool_args={"symbol": "X"}, status="pending")
print(f"  ✓ 创建 audit #{audit_id}")
update_write_status(None, audit_id, status="confirmed", confirmed_by="test_user")
items = list_writes(limit=5)
assert any(it["id"] == audit_id and it["status"] == "confirmed" for it in items)
print(f"  ✓ 更新 status 后 list_writes 正确反映")

# 非法 status 应该 raise
try:
    update_write_status(None, audit_id, status="bogus")
    assert False, "should raise"
except ValueError:
    pass
print("  ✓ 非法 status 校验")

# [D] orchestrator HITL 检测
print("\n[D] orchestrator _emit_message HITL:")
awc = ToolMessage(
    content='AWAITING_CONFIRMATION: {"tool_name": "create_alert", "tool_args": {"symbol": "X"}, "impact": "创建告警", "audit_id": 999}',
    tool_call_id="call_x",
)
events = list(_emit_message(awc))
assert events[0][0] == "confirm_request"
assert events[0][1]["audit_id"] == 999
print(f"  ✓ 检测 AWAITING_CONFIRMATION → confirm_request event")

normal = ToolMessage(content="NOTE_CREATED: ok", tool_call_id="call_y")
events2 = list(_emit_message(normal))
assert events2[0][0] == "tool_result"
print(f"  ✓ 普通 ToolMessage → tool_result event")

# [E] validate_write_intent
print("\n[E] validate_write_intent 一体化:")
is_w, impact = validate_write_intent("create_alert", {"symbol": "X"})
assert is_w == True
assert "告警" in impact or "通知" in impact
print(f"  ✓ create_alert: is_write={is_w}, impact={impact[:40]}...")

print()
print("=" * 60)
print("✅ Day 3 全部测试通过")
print("=" * 60)
