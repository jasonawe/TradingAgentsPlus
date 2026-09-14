"""Observability — P7 (v3 spec §7.2 #1-#10).

Exposes:
- AuditLogger — write audit log (JSONL)
- Metrics — token / latency / error counters
- HealthChecker — /api/harness/health payload
- FeishuAlerter — 飞书群机器人 webhook
- ProviderFailover — fall-back to next provider on transient errors
"""
from .audit import AuditLogger
from .failover import ProviderFailover
from .health import HealthChecker
from .metrics import Metrics
from .tracing import Tracer

__all__ = [
    "AuditLogger",
    "ProviderFailover",
    "HealthChecker",
    "Metrics",
    "Tracer",
]
from .feishu import FeishuAlerter

__all__ += ["FeishuAlerter"]
