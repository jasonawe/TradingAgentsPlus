# Pull-Eval (Background LLM Judge) — Step 43

## Background

当前 L3 LLM judge 在 `_run_l3_judge` 里 **同步** 调用，发生在
`synthesize → agent_final` 之后。User 必须等 judge LLM call 完成
才能看到 final answer — 通常多 1.5–3s latency。

## 设计

`realtime_harness` 的优先级是核心，前置 OpenAI push-eval 改为
background pull-eval：

- `agent_final` 立即 emit (用户立即看到答案)
- 同时启动 background task 跑 L3 judge
- background task 完成后 emit `verified_late` event 注入 SSE stream
- 前端收到 `verified_late` 后更新 verification badge

实现：

- `BackgroundTaskQueue` 类: per-session FIFO 队列
- orchestrator._stream_chat 在 finalize 后不 await background tasks
- new `_emit_late()` hook: 把 background events 推入 queue
- Web SSE stream generator: 监听 queue 输出 background events

## 文件

1. `tradingagents/agent_harness/core/background_queue.py` (新)
2. `tradingagents/agent_harness/core/orchestrator.py` — pull eval
3. `web/app.py` — SSE stream 监听 background queue
4. `tests/test_step43_pull_eval.py` — 8 测试
