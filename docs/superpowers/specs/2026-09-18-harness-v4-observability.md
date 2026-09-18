# Observability Hooks — Step 44

## Background

Harness 现在 emit SSE events but 无统一 tracing/metrics：
- 无法看单个 turn 的端到端 latency 分解
- 无 metrics counter / histogram
- 失败时无 request_id 串联 trace

## 设计

`tradingagents/agent_harness/observability/` 子模块：

- `tracing.py` — `Tracer` 类 + `Span` dataclass，per-session trace tree
- `metrics.py` — `Counter` / `Histogram` / `Gauge`，in-memory + 可选 Prometheus export
- `hooks.py` — emit hook: 每个 SSE event 自动 emit `span_event` 给 Tracer

集成点：

- Orchestrator._stream_chat 创建 root span，每个 emit 是 child span
- /api/harness/observability/traces 端点返回最近 traces
- /api/harness/observability/metrics 返回 aggregate metrics

## 文件

1. `tradingagents/agent_harness/observability/tracing.py` (新)
2. `tradingagents/agent_harness/observability/metrics.py` (新)
3. `tradingagents/agent_harness/observability/__init__.py`
4. `tradingagents/agent_harness/observability/hooks.py` — emit hook
5. `web/app.py` — observability endpoints
6. `tests/test_step44_observability.py` — 9 测试
