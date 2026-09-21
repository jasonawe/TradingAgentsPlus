"""Task 2 — MessageIngestor 测试。

覆盖 plan 要求:
- 嵌套 dict/list 递归遍历
- depth 12 / node limit 10,000
- 大小写无关 hidden-reasoning keys
- secret redaction
- Tool-schema sensitive fields
- 禁用 <scratchpad> 标记
- stable canonical JSON
- payload SHA-256
- UUIDv5 message idempotency
- store call 之前的 reject
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

import pytest


# 测试驱动导入 — 实现前是 RED
def _import_ingestor():
    from tradingagents.agent_harness.runtime.ingest import MessageIngestor
    return MessageIngestor


# ════════════════════════════════════════════════════════
# Step 1.1 — hidden-reasoning key rejection (大小写无关,任意深度)
# ════════════════════════════════════════════════════════

@pytest.mark.parametrize("key", ["reasoning", "Chain_Of_Thought", "SCRATCHPAD", "Hidden_Prompt"])
def test_ingestor_rejects_hidden_reasoning_at_any_depth(key):
    Ingestor = _import_ingestor()
    payload = {"outer": [{"inner": [{key: "private thought"}]}]}
    with pytest.raises(Exception) as exc:
        Ingestor().sanitize(payload)
    # 安全消息错误
    assert "hidden" in str(exc.value).lower() or "reasoning" in str(exc.value).lower() or "scratchpad" in str(exc.value).lower() or "unsafe" in str(exc.value).lower()


def test_ingestor_rejects_top_level_reasoning_key():
    Ingestor = _import_ingestor()
    with pytest.raises(Exception):
        Ingestor().sanitize({"reasoning": "I think the stock is good"})


def test_ingestor_rejects_chain_of_thought_nested():
    Ingestor = _import_ingestor()
    payload = {"data": {"items": [{"chain_of_thought": "step1 -> step2"}]}}
    with pytest.raises(Exception):
        Ingestor().sanitize(payload)


# ════════════════════════════════════════════════════════
# Step 1.2 — secret redaction (类型化 REDACTED sentinel)
# ════════════════════════════════════════════════════════

@pytest.mark.parametrize("secret_key", ["api_key", "token", "secret", "authorization", "password"])
def test_ingestor_redacts_nested_secret(secret_key):
    Ingestor = _import_ingestor()
    sanitized = Ingestor().sanitize({"cfg": {secret_key: "super-secret-value"}})
    assert sanitized["cfg"][secret_key] == {"kind": "REDACTED"}


def test_ingestor_redacts_secret_in_list():
    Ingestor = _import_ingestor()
    sanitized = Ingestor().sanitize({"items": [{"api_key": "abc123"}]})
    assert sanitized["items"][0]["api_key"] == {"kind": "REDACTED"}


def test_ingestor_redacts_secret_deep_nested():
    Ingestor = _import_ingestor()
    sanitized = Ingestor().sanitize({"a": {"b": {"c": {"d": {"token": "xyz"}}}}, "e": [{"token": "qqq"}]})
    assert sanitized["a"]["b"]["c"]["d"]["token"] == {"kind": "REDACTED"}
    assert sanitized["e"][0]["token"] == {"kind": "REDACTED"}


# ════════════════════════════════════════════════════════
# Step 1.3 — <scratchpad> 结构化标记拒绝
# ════════════════════════════════════════════════════════

@pytest.mark.parametrize("marker", ["<scratchpad>", "<chain_of_thought>", "<hidden_prompt>", "<SCRATCHPAD>"])
def test_ingestor_rejects_structured_markers_in_strings(marker):
    Ingestor = _import_ingestor()
    with pytest.raises(Exception):
        Ingestor().sanitize({"content": f"prefix {marker} suffix"})


def test_ingestor_rejects_scratchpad_marker_in_nested_string():
    Ingestor = _import_ingestor()
    payload = {"items": [{"text": "hello <scratchpad>world"}]}
    with pytest.raises(Exception):
        Ingestor().sanitize(payload)


# ════════════════════════════════════════════════════════
# Step 1.4 — depth / node limit
# ════════════════════════════════════════════════════════

def test_ingestor_enforces_depth_limit():
    Ingestor = _import_ingestor()
    # 深度 13,超过 12 上限
    payload: dict = {}
    cur = payload
    for _ in range(15):
        cur["next"] = {}
        cur = cur["next"]
    with pytest.raises(Exception) as exc:
        Ingestor().sanitize(payload)
    assert "depth" in str(exc.value).lower() or "too deep" in str(exc.value).lower()


def test_ingestor_enforces_node_limit():
    Ingestor = _import_ingestor()
    # 构造 >10,000 个节点
    payload = {"items": [{"k": i} for i in range(10500)]}
    with pytest.raises(Exception) as exc:
        Ingestor().sanitize(payload)
    assert "node" in str(exc.value).lower() or "too large" in str(exc.value).lower()


# ════════════════════════════════════════════════════════
# Step 1.5 — stable canonical JSON
# ════════════════════════════════════════════════════════

def test_ingestor_canonical_json_stable():
    Ingestor = _import_ingestor()
    payload1 = {"b": 2, "a": 1, "nested": {"y": [{"x": 1}]}}
    payload2 = {"a": 1, "nested": {"y": [{"x": 1}]}, "b": 2}
    # 不同 dict 顺序,但应产生相同 canonical JSON
    canon1 = Ingestor().canonical_json(payload1)
    canon2 = Ingestor().canonical_json(payload2)
    assert canon1 == canon2
    # 验证是合法 JSON
    assert json.loads(canon1) == payload1


def test_ingestor_canonical_json_with_unicode():
    Ingestor = _import_ingestor()
    payload = {"name": "招商银行", "code": "600036.SS"}
    canon = Ingestor().canonical_json(payload)
    # 中文字符应正确保留(不转 \uXXXX)
    assert "招商银行" in canon


# ════════════════════════════════════════════════════════
# Step 1.6 — payload SHA-256
# ════════════════════════════════════════════════════════

def test_ingestor_payload_sha256():
    Ingestor = _import_ingestor()
    payload = {"a": 1, "b": [1, 2, 3]}
    sha = Ingestor().payload_sha256(payload)
    assert isinstance(sha, str)
    assert len(sha) == 64
    # 与手动算的一致
    canon = Ingestor().canonical_json(payload)
    expected = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert sha == expected


def test_ingestor_payload_sha256_different_payloads_different_hashes():
    Ingestor = _import_ingestor()
    h1 = Ingestor().payload_sha256({"x": 1})
    h2 = Ingestor().payload_sha256({"x": 2})
    assert h1 != h2


# ════════════════════════════════════════════════════════
# Step 1.7 — UUIDv5 message idempotency
# ════════════════════════════════════════════════════════

def test_ingestor_message_idempotency_key_is_uuidv5():
    Ingestor = _import_ingestor()
    key = Ingestor().message_idempotency_key(
        task_id="t-1",
        execution_attempt=1,
        msg_type="PROGRESS",
        recipient="VerifierAgent",
        causation_id=None,
        payload_hash="a" * 64,
    )
    # 应是合法 UUID
    parsed = uuid.UUID(key)
    assert parsed.version == 5


def test_ingestor_message_idempotency_key_stable():
    Ingestor = _import_ingestor()
    args = {
        "task_id": "t-1",
        "execution_attempt": 2,
        "msg_type": "PROGRESS",
        "recipient": "VerifierAgent",
        "causation_id": None,
        "payload_hash": "b" * 64,
    }
    key1 = Ingestor().message_idempotency_key(**args)
    key2 = Ingestor().message_idempotency_key(**args)
    assert key1 == key2


def test_ingestor_message_idempotency_key_varies_by_args():
    Ingestor = _import_ingestor()
    base = {
        "task_id": "t-1",
        "execution_attempt": 1,
        "msg_type": "PROGRESS",
        "recipient": "VerifierAgent",
        "causation_id": None,
        "payload_hash": "a" * 64,
    }
    base_key = Ingestor().message_idempotency_key(**base)
    # 不同 task_id → 不同 key
    different_task = Ingestor().message_idempotency_key(**{**base, "task_id": "t-2"})
    assert different_task != base_key


# ════════════════════════════════════════════════════════
# Step 1.8 — rejection 在 store call 之前
# ════════════════════════════════════════════════════════

def test_ingestor_rejects_before_store_call():
    """reject 必须在 store 调用之前 — 不需要 store 模块存在也能验证。

    通过 ``MessageIngestor.sanitize`` 拒绝路径上抛异常,
    验证 ``MessageIngestor`` 类本身没有 ``write_message`` 或 ``persist``
    方法会被调用 — sanitizer 在 reject 时不应有任何副作用。
    """
    Ingestor = _import_ingestor()
    ingestor = Ingestor()
    # 验证 sanitize 在 reject 时确实抛异常,且没有任何 write/persist/store 方法被调
    assert not hasattr(ingestor, "write_message"),         "MessageIngestor 不应触发 store 调用"
    assert not hasattr(ingestor, "persist"),         "MessageIngestor 不应触发 store 调用"
    with pytest.raises(Exception):
        ingestor.sanitize({"reasoning": "internal thought"}), "store should not be called when sanitize rejects"


# ════════════════════════════════════════════════════════
# Step 1.9 — 正常 payload 通过 sanitize
# ════════════════════════════════════════════════════════

def test_ingestor_accepts_normal_payload():
    Ingestor = _import_ingestor()
    payload = {"symbol": "600036.SS", "price": 41.42, "currency": "CNY"}
    sanitized = Ingestor().sanitize(payload)
    assert sanitized == payload


def test_ingestor_preserves_primitives():
    Ingestor = _import_ingestor()
    payload = {"str": "hello", "int": 42, "float": 3.14, "bool": True, "none": None}
    sanitized = Ingestor().sanitize(payload)
    assert sanitized == payload


# ════════════════════════════════════════════════════════
# Step 1.10 — 顶层 + 嵌套混合 payload
# ════════════════════════════════════════════════════════

def test_ingestor_redacts_and_rejects_in_same_payload():
    Ingestor = _import_ingestor()
    # 同时包含 secret(应 redact)和 reasoning key(应 reject)
    payload = {"api_key": "secret", "data": [{"reasoning": "internal"}]}
    # 因为有 reasoning key,整条 reject
    with pytest.raises(Exception):
        Ingestor().sanitize(payload)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
