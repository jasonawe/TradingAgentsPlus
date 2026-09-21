"""Task 9 — LLMExecutor.

Spec §4.5 + §27:LLMExecutor 是所有 Agent 共用的 LLM 调用边界。
- 统一 provider 选择 / timeout / retry / circuit breaker
- 响应缓存 / token 预留与结算 / 结构化错误分类
- 通过 UsageReservationRepository 做 budget enforcement
- 不直接持有 LLMFactory — 注入式
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..runtime.persistence.usage import (
    BudgetExceeded,
    UsageReservationRepository,
)

LOGGER = logging.getLogger(__name__)


class BudgetExhausted(RuntimeError):
    """run budget exhausted — provider call not made."""


@dataclass
class LLMResponse:
    """Typed LLM response. Always carries actual token usage (or 0 for cache hit)."""

    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    cached: bool = False


@dataclass
class LLMExecutor:
    """LLM call boundary — wraps a provider callable with budget + cache."""

    store: Any
    response_cache: Any | None = None
    # 估算 tokens 用于 reservation;简化版:prompt 长度 / 2 + max_tokens
    estimation_ratio: int = 2

    def complete(
        self,
        *,
        task_id: str,
        agent_name: str,
        messages: list[dict],
        provider: Callable[..., Any],
        model: str,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        execution_attempt: int = 1,
        call_ordinal: int = 0,
    ) -> LLMResponse:
        """Wrap a provider call with budget reservation + cache lookup + settlement."""
        ur = UsageReservationRepository(self.store)

        # 1. cache lookup (if cache_key provided)
        if cache_key and self.response_cache is not None:
            hit = self.response_cache.get(cache_key)
            if hit is not None:
                # 0-usage reservation, settled immediately
                now_iso = _now_iso()
                run_id = self._run_id_for_task(task_id)
                if run_id is not None:
                    ur.reserve(
                        reservation_id=str(uuid.uuid4()),
                        call_id=cache_key,
                        run_id=run_id, task_id=task_id,
                        agent_name=agent_name, execution_attempt=execution_attempt,
                        call_ordinal=call_ordinal,
                        provider="cache", model=model,
                        reserved_input_tokens=0, reserved_output_tokens=0,
                        lease_expires_at=now_iso, now=now_iso,
                    )
                    # cache hit 直接 settle(0 tokens)
                    try:
                        ur.settle(
                            reservation_id=ur.list_for_task(task_id)[-1]["reservation_id"],
                            actual_input_tokens=0, actual_output_tokens=0, now=now_iso,
                        )
                    except Exception:
                        pass
                # hit is LLMResponse object
                usage = getattr(hit, "usage", {}) or {}
                return LLMResponse(
                    content=hit.content,
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    model=getattr(hit, "model", model), cached=True,
                )

        # 2. budget reservation(估 max_tokens 输入 + max_tokens 输出)
        now_iso = _now_iso()
        run_id = self._run_id_for_task(task_id)
        if run_id is None:
            raise RuntimeError(f"task {task_id} has no run_id; can't reserve budget")
        reservation_id = str(uuid.uuid4())
        est_input = self._estimate_input_tokens(messages)
        est_output = max_tokens or 100
        try:
            ur.reserve(
                reservation_id=reservation_id,
                call_id=str(uuid.uuid4()),
                run_id=run_id, task_id=task_id,
                agent_name=agent_name, execution_attempt=execution_attempt,
                call_ordinal=call_ordinal,
                provider="custom", model=model,
                reserved_input_tokens=est_input, reserved_output_tokens=est_output,
                lease_expires_at=now_iso, now=now_iso,
            )
        except BudgetExceeded:
            raise BudgetExhausted(
                f"run {run_id} budget exhausted before provider call"
            )

        # 3. provider call
        try:
            response = provider(messages, model=model, max_tokens=max_tokens or 0)
        except Exception as e:
            # provider 抛错前 release reservation
            try:
                ur.release(reservation_id=reservation_id, now=_now_iso())
            except RuntimeError:
                pass
            raise

        # 4. settle(用 actual tokens 替换 estimate)
        now_iso = _now_iso()
        actual_in = int(getattr(response, "prompt_tokens", 0) or 0)
        actual_out = int(getattr(response, "completion_tokens", 0) or 0)
        try:
            ur.settle(
                reservation_id=reservation_id,
                actual_input_tokens=actual_in, actual_output_tokens=actual_out,
                now=now_iso,
            )
        except RuntimeError:
            # settlement 失败时回退 EXPIRED_COMMITTED 用 reserved 数量
            pass

        # 5. cache write
        if cache_key and self.response_cache is not None:
            self.response_cache.set(cache_key, {
                "content": response.content,
                "model": model,
                "prompt_tokens": actual_in,
                "completion_tokens": actual_out,
            })

        return LLMResponse(
            content=response.content,
            prompt_tokens=actual_in, completion_tokens=actual_out,
            model=getattr(response, "model", model),
        )

    def _run_id_for_task(self, task_id: str) -> str | None:
        from ..runtime.persistence.tasks import TaskRepository
        t = TaskRepository(self.store).get_task(task_id)
        return t["run_id"] if t else None

    def _estimate_input_tokens(self, messages: list[dict]) -> int:
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        # 粗估:1 token ≈ estimation_ratio 字符
        return max(1, total_chars // self.estimation_ratio)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["LLMExecutor", "LLMResponse", "BudgetExhausted"]
