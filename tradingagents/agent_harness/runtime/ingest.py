"""Task 2 — MessageIngestor。

负责:
1. 递归遍历 dict/list,depth ≤ 12、node ≤ 10,000
2. 大小写无关 hidden-reasoning key denylist(reasoning / chain_of_thought /
   scratchpad / hidden_prompt)→ 整条 reject
3. secret key denylist(api_key / token / secret / authorization / password)→
   替换为 ``{"kind": "REDACTED"}``
4. 字符串里的 <scratchpad> / <chain_of_thought> / <hidden_prompt> 标记 → reject
5. Pydantic schema 校验 + canonical JSON + payload SHA-256
6. UUIDv5 message idempotency key
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any


class UnsafeMessageError(ValueError):
    """消息包含被禁内容(hide reasoning key、scratchpad 标记)。"""


class MessageTooLargeError(ValueError):
    """消息超过 depth / node 上限。"""


MAX_DEPTH = 12
MAX_NODES = 10_000

HIDDEN_REASONING_KEYS = frozenset({
    "reasoning",
    "chain_of_thought",
    "scratchpad",
    "hidden_prompt",
})

SECRET_KEYS = frozenset({
    "api_key",
    "token",
    "secret",
    "authorization",
    "password",
})

FORBIDDEN_MARKERS = re.compile(
    r"<\s*(?:scratchpad|chain_of_thought|hidden_prompt)\s*>",
    re.IGNORECASE,
)

MESSAGE_IDEMPOTENCY_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-deadbeef0001")


class MessageIngestor:
    """Agent 消息预处理器 — sanitize + canonicalize + idempotency。"""

    def sanitize(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UnsafeMessageError(
                f"top-level payload must be dict, got {type(payload).__name__}"
            )

        counter = {"n": 0}
        result = self._walk(payload, depth=0, counter=counter)
        return result

    def _walk(self, node: Any, depth: int, counter: dict) -> Any:
        if depth > MAX_DEPTH:
            raise MessageTooLargeError(f"payload too deep (> {MAX_DEPTH})")

        counter["n"] += 1
        if counter["n"] > MAX_NODES:
            raise MessageTooLargeError(f"payload too large (> {MAX_NODES} nodes)")

        if isinstance(node, dict):
            return self._walk_dict(node, depth, counter)
        if isinstance(node, list):
            return [self._walk(item, depth, counter) for item in node]
        if isinstance(node, str):
            return self._check_string(node)
        return node

    def _walk_dict(self, d: dict, depth: int, counter: dict) -> dict:
        out: dict = {}
        for key, value in d.items():
            if not isinstance(key, str):
                raise UnsafeMessageError(
                    f"non-string key in payload: {type(key).__name__}"
                )

            key_lower = key.lower()
            if key_lower in HIDDEN_REASONING_KEYS:
                raise UnsafeMessageError(
                    f"hidden-reasoning key rejected: {key!r} "
                    f"(denylist: {sorted(HIDDEN_REASONING_KEYS)})"
                )

            if key_lower in SECRET_KEYS:
                out[key] = {"kind": "REDACTED"}
                continue

            out[key] = self._walk(value, depth + 1, counter)
        return out

    def _check_string(self, s: str) -> str:
        if FORBIDDEN_MARKERS.search(s):
            match = FORBIDDEN_MARKERS.search(s)
            raise UnsafeMessageError(
                f"forbidden structured marker in string: {match.group(0)!r}"
            )
        return s

    def canonical_json(self, payload: Any) -> str:
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    def payload_sha256(self, payload: Any) -> str:
        canon = self.canonical_json(payload)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def message_idempotency_key(
        self,
        *,
        task_id: str,
        execution_attempt: int,
        msg_type: str,
        recipient: str,
        causation_id: str | None,
        payload_hash: str,
    ) -> str:
        identity = (
            f"{task_id}|{execution_attempt}|{msg_type}|{recipient}"
            f"|{causation_id or ''}|{payload_hash}"
        )
        return str(uuid.uuid5(MESSAGE_IDEMPOTENCY_NAMESPACE, identity))


__all__ = [
    "MessageIngestor",
    "UnsafeMessageError",
    "MessageTooLargeError",
    "MAX_DEPTH",
    "MAX_NODES",
    "HIDDEN_REASONING_KEYS",
    "SECRET_KEYS",
]
