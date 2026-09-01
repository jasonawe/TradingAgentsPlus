"""Stable error-code taxonomy for web analysis failures.

The catalog is shared by the manager, runner, classifier, and the browser
client. Terminal reasons written to ``web_runs.terminal_reason`` are the
keys of :data:`TERMINAL_REASONS` so audit logs stay machine-stable.
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class TerminalReason(str, Enum):
    """Stable, machine-readable reasons a run ended."""

    # Worker lease expired: the in-process lease thread stopped renewing
    # the worker heartbeat while the deadline was still in the future.
    WORKER_LEASE_EXPIRED = "worker_lease_expired"
    # The fixed deadline (run_timeout_seconds) elapsed.
    RUN_DEADLINE_EXCEEDED = "run_deadline_exceeded"
    # Service restarted while a run was queued/running.
    SERVICE_RESTARTED = "service_restarted"
    # User-initiated cancellation.
    CANCELLED = "cancelled"
    # Clean completion.
    COMPLETED = "completed"
    # Provider / model error categories.
    MODEL_TIMEOUT = "model_timeout"
    MODEL_RATE_LIMITED = "model_rate_limited"
    MODEL_AUTH_ERROR = "model_auth_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    DATA_SOURCE_TIMEOUT = "data_source_timeout"
    DATA_SOURCE_UNAVAILABLE = "data_source_unavailable"
    # Catch-all.
    WORKER_ERROR = "worker_error"
    PUBLISH_INCOMPLETE = "publish_incomplete"


TERMINAL_REASONS: Final[frozenset[str]] = frozenset(member.value for member in TerminalReason)

# Mapping from stable reason to the public terminal status written on
# web_runs.status. Keys must stay in sync with web.models.RunStatus.
TERMINAL_STATUS_BY_REASON: Final[dict[str, str]] = {
    TerminalReason.WORKER_LEASE_EXPIRED.value: "interrupted",
    TerminalReason.SERVICE_RESTARTED.value: "interrupted",
    TerminalReason.RUN_DEADLINE_EXCEEDED.value: "timed_out",
    TerminalReason.CANCELLED.value: "cancelled",
    TerminalReason.COMPLETED.value: "completed",
    TerminalReason.MODEL_TIMEOUT.value: "failed",
    TerminalReason.MODEL_RATE_LIMITED.value: "failed",
    TerminalReason.MODEL_AUTH_ERROR.value: "failed",
    TerminalReason.MODEL_UNAVAILABLE.value: "failed",
    TerminalReason.DATA_SOURCE_TIMEOUT.value: "failed",
    TerminalReason.DATA_SOURCE_UNAVAILABLE.value: "failed",
    TerminalReason.WORKER_ERROR.value: "failed",
    TerminalReason.PUBLISH_INCOMPLETE.value: "failed",
}


# Reasons where a retry from the saved checkpoint is allowed (assuming the
# checkpoint still exists and signatures match).
RETRYABLE_REASONS: Final[frozenset[str]] = frozenset(
    {
        TerminalReason.MODEL_TIMEOUT.value,
        TerminalReason.MODEL_RATE_LIMITED.value,
        TerminalReason.MODEL_UNAVAILABLE.value,
        TerminalReason.DATA_SOURCE_TIMEOUT.value,
        TerminalReason.DATA_SOURCE_UNAVAILABLE.value,
        TerminalReason.WORKER_LEASE_EXPIRED.value,
        TerminalReason.SERVICE_RESTARTED.value,
        TerminalReason.RUN_DEADLINE_EXCEEDED.value,
    }
)


# Stable reasons that indicate the LLM/data call itself failed. Used by
# the retry-link UI to decide which retry button to show.
PROVIDER_FAILURE_REASONS: Final[frozenset[str]] = frozenset(
    {
        TerminalReason.MODEL_TIMEOUT.value,
        TerminalReason.MODEL_RATE_LIMITED.value,
        TerminalReason.MODEL_AUTH_ERROR.value,
        TerminalReason.MODEL_UNAVAILABLE.value,
        TerminalReason.DATA_SOURCE_TIMEOUT.value,
        TerminalReason.DATA_SOURCE_UNAVAILABLE.value,
    }
)


# Safe, user-facing Chinese prompt templates keyed by reason. These are
# what gets stored in web_runs.error_message and shown to the user. Keep
# them short and free of internal model names.
USER_MESSAGES: Final[dict[str, str]] = {
    TerminalReason.MODEL_TIMEOUT.value: "模型响应超时，请稍后重试",
    TerminalReason.MODEL_RATE_LIMITED.value: "模型服务请求过于频繁，请稍后重试",
    TerminalReason.MODEL_AUTH_ERROR.value: "模型服务认证失败，请检查配置",
    TerminalReason.MODEL_UNAVAILABLE.value: "模型服务暂时不可用，请稍后重试",
    TerminalReason.DATA_SOURCE_TIMEOUT.value: "行情或研究数据源响应超时",
    TerminalReason.DATA_SOURCE_UNAVAILABLE.value: "必需的数据源不可用",
    TerminalReason.WORKER_LEASE_EXPIRED.value: "分析执行器失去连接",
    TerminalReason.SERVICE_RESTARTED.value: "Web 服务在分析过程中重启",
    TerminalReason.RUN_DEADLINE_EXCEEDED.value: "分析超过最长运行时间",
    TerminalReason.CANCELLED.value: "分析已取消",
    TerminalReason.PUBLISH_INCOMPLETE.value: "报告发布未完成",
    TerminalReason.WORKER_ERROR.value: "分析执行发生未分类错误",
    TerminalReason.COMPLETED.value: "分析完成",
}


__all__ = [
    "PROVIDER_FAILURE_REASONS",
    "RETRYABLE_REASONS",
    "TERMINAL_REASONS",
    "TERMINAL_STATUS_BY_REASON",
    "USER_MESSAGES",
    "TerminalReason",
]
