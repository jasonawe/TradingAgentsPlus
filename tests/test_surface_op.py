"""Q7 / P2-10 — SurfaceOp 命名空间。

覆盖:
  - derive_history 等价 chat_history
  - commit_system_prompt idempotent
  - replace_message 替换 user/assistant/tool event 的 content
  - replace_message 多 field 替换
  - replace_message 拒绝非 replaceable type (e.g. write_audit_log)
  - replace_message 拒绝 missing event
  - replace_field 单字段便捷
  - undo_replace 还原到 replace 链前一个值
  - undo_replace 拒绝非 replace event
"""
from __future__ import annotations

import pytest


@pytest.fixture
def event_log(tmp_path):
    from tradingagents.agent_harness.memory.event_log import EventLog
    return EventLog(db_path=tmp_path / "events.sqlite")


@pytest.fixture
def op(event_log):
    from tradingagents.agent_harness.memory.surface_op import SurfaceOp
    return SurfaceOp(event_log)


def test_derive_history_equivalent_to_chat_history(op, event_log):
    op.append("s1", "user/message", {"role": "user", "content": "hello"})
    op.append("s1", "assistant/message", {"role": "assistant", "content": "hi"})
    h_op = op.derive_history("s1", apply_replaces=True)
    h_raw = event_log.chat_history("s1", apply_replaces=True)
    assert h_op == h_raw


def test_commit_system_prompt_idempotent(op, event_log):
    op.commit_system_prompt("s1", "you are an analyst v1")
    op.commit_system_prompt("s1", "you are an analyst v2")
    # Underlying log: 1 system/message + 1 replace event (audit chain)
    raw_events = list(event_log.events("s1"))
    assert any(e.type == "system/message" for e in raw_events)
    assert any(e.type == "replace" for e in raw_events)
    # with apply_replaces=True, only the latest content surfaces
    rendered = op.derive_history("s1", apply_replaces=True)
    sys_messages = [m for m in rendered if m["role"] == "system"]
    assert len(sys_messages) == 1
    assert sys_messages[0]["content"] == "you are an analyst v2"


def test_replace_message_updates_user_content(op, event_log):
    ev = op.append("s1", "user/message", {"role": "user", "content": "buy AAPL?"})
    rep = op.replace_message("s1", ev.seq, "AAPL 现在多少钱?", reason="redact name")
    assert rep.type == "replace"
    assert rep.data["target_seq"] == ev.seq
    assert rep.data["new_data"]["content"] == "AAPL 现在多少钱?"
    assert rep.data["reason"] == "redact name"
    # chat_history sees new content
    rendered = op.derive_history("s1", apply_replaces=True)
    user_msgs = [m for m in rendered if m["role"] == "user"]
    assert user_msgs[-1]["content"] == "AAPL 现在多少钱?"


def test_replace_message_preserves_other_fields_in_audit(op, event_log):
    """Replace mutates new_data but the underlying chain still has both old and new.
    Verifies that the replace event's new_data preserves untouched fields."""
    ev = op.append("s1", "tool/result", {
        "name": "get_quote", "content": "secret-data", "extra": "keep-me",
    })
    rep = op.replace_message("s1", ev.seq, "redacted-data", reason="PII")
    assert rep.data["new_data"]["content"] == "redacted-data"
    assert rep.data["new_data"]["extra"] == "keep-me"  # other field preserved in audit


def test_replace_message_multi_field_in_audit(op):
    ev = op.append("s1", "assistant/message", {
        "role": "assistant", "content": "v1", "model": "m1", "tokens": 100,
    })
    rep = op.replace_message(
        "s1", ev.seq,
        {"content": "v2", "model": "m2"},
        fields=("content", "model"),
    )
    # Audit records all three fields; "content" and "model" updated, "tokens" preserved.
    assert rep.data["new_data"]["content"] == "v2"
    assert rep.data["new_data"]["model"] == "m2"
    assert rep.data["new_data"]["tokens"] == 100


def test_replace_message_rejects_audit_event(op):
    op.append("s1", "write_audit_log", {"action": "create_note"})
    # find the audit seq
    audit = next(e for e in op.surface_events("s1") if e.type == "write_audit_log") if False else None
    # write_audit_log is LOG surface, not in REPLACEABLE_TYPES; direct get:
    from tradingagents.agent_harness.memory.surface_op import SurfaceOpError
    audit_ev = op._log.append("s1", "write_audit_log", {"action": "create_note"})
    with pytest.raises(SurfaceOpError, match="not replaceable"):
        op.replace_message("s1", audit_ev.seq, "tampered", reason="hack")


def test_replace_message_rejects_missing_seq(op):
    from tradingagents.agent_harness.memory.surface_op import SurfaceOpError
    with pytest.raises(SurfaceOpError, match="not found"):
        op.replace_message("s1", 9999, "x", reason="x")


def test_replace_field_convenience(op):
    ev = op.append("s1", "assistant/message", {
        "role": "assistant", "content": "v1", "model": "gpt-4",
    })
    rep = op.replace_field("s1", ev.seq, "model", "gpt-5", reason="upgrade")
    assert rep.data["new_data"]["model"] == "gpt-5"
    assert rep.data["new_data"]["content"] == "v1"


def test_undo_replace_restores_previous_value(op):
    """undo_replace 把 replace 链前一个值恢复成最新。"""
    from tradingagents.agent_harness.memory.event_log import EventLog
    ev = op.append("s1", "user/message", {"role": "user", "content": "v1"})
    rep1 = op.replace_message("s1", ev.seq, "v2", reason="edit1")
    # undo rep1 → content should go back to "v1"
    undo = op.undo_replace("s1", rep1.seq)
    rendered = op.derive_history("s1", apply_replaces=True)
    user_msgs = [m for m in rendered if m["role"] == "user"]
    assert user_msgs[-1]["content"] == "v1"
    # audit chain visible
    assert undo.type == "replace"
    assert undo.data["reason"].startswith("undo replace")


def test_undo_replace_rejects_non_replace_event(op):
    from tradingagents.agent_harness.memory.surface_op import SurfaceOpError
    ev = op.append("s1", "user/message", {"role": "user", "content": "hi"})
    with pytest.raises(SurfaceOpError, match="not a replace"):
        op.undo_replace("s1", ev.seq)


def test_replace_chain_walks_through_multiple_replaces(op):
    """连续 3 次 replace,chat_history 看到最后一次的值。"""
    ev = op.append("s1", "user/message", {"role": "user", "content": "v1"})
    op.replace_message("s1", ev.seq, "v2", reason="r2")
    op.replace_message("s1", ev.seq, "v3", reason="r3")
    op.replace_message("s1", ev.seq, "v4", reason="r4")
    rendered = op.derive_history("s1", apply_replaces=True)
    user_msgs = [m for m in rendered if m["role"] == "user"]
    assert user_msgs[-1]["content"] == "v4"


def test_get_event_returns_underlying_event(op):
    ev = op.append("s1", "user/message", {"role": "user", "content": "hi"})
    fetched = op.get_event("s1", ev.seq)
    assert fetched.seq == ev.seq
    assert fetched.type == "user/message"
    assert op.get_event("s1", 9999) is None
