# Multi-Intent Fan-Out Aggregator — 2026-09-18 (Step 41)

## Background

v3 review backlog item D. `Orchestrator._run_multi` 串行执行多 intent：
每个 symbol 拉 quote → 等 → 拉 fundamentals → 等 → ... 总时间 = sum。
fan-out primitive (Step 37) 已就位 — 现在用 fan-out 并行执行 multi-intent。

## 设计

`_run_multi` 改造：

- 每个 intent 走独立的 FanOut children (quote + news + fundamentals)
- `asyncio.gather` 收集所有 results
- 总时间 = max(每个 symbol 的 fetch 时间)

向后兼容：
- 单 intent 仍走原路径
- Multi-intent 走 fan-out path
- Return 顺序保持 source order (asyncio.gather preserves order)

性能：
- 3 symbols × 3 sources 串行：~ 9 × 100ms = 900ms
- 并行：~ 3 × 100ms = 300ms (3x speedup)

## 文件

1. `tradingagents/agent_harness/core/multi_intent_workflow.py` (新) — FanOut-based
2. `tradingagents/agent_harness/core/orchestrator.py` — `_run_multi` 优先
   用新 workflow，失败回退到老 path
3. `tests/test_step41_multi_intent_fanout.py` — 6 测试
