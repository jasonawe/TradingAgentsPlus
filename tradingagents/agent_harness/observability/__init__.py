"""Observability — P7 (v3 spec §7.2 #1-#10).

Exposes:
- AuditLogger — write audit log (JSONL)
- Metrics — token / latency / error counters
- HealthChecker — /api/harness/health payload
- FeishuAlerter — 飞书群机器人 webhook
- ProviderFailover — fall-back to next provider on transient errors
- Tracer — per-session tracing (Step 44)
- Counter / Histogram / Gauge / MetricsRegistry — Step 44 metrics
"""
from .audit import AuditLogger
from .failover import ProviderFailover
from .health import HealthChecker
from .metrics import Metrics
from .tracing import Tracer, Span, current_trace_id
from .feishu import FeishuAlerter
from .metrics_new import (
    Counter, Histogram, Gauge, MetricsRegistry,
)

__all__ = [
    "AuditLogger",
    "ProviderFailover",
    "HealthChecker",
    "Metrics",
    "Tracer",
    "Span",
    "current_trace_id",
    "FeishuAlerter",
    "Counter",
    "Histogram",
    "Gauge",
    "MetricsRegistry",
]
