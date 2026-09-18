# Session RWLock — 2026-09-18 (Step 39)

## Background

v3 review backlog item A. Current `SessionLockManager` 用 `asyncio.Lock`
(mutex): 同 session 任何两个请求必须串行。实际使用中：

- Frontend 多个 SSE subscriber (chat bubble + audit viewer + watchlist
  tab) 想监听同一 session — 都被 mutex 阻塞
- LLM judge 想读 session plan / audit / metadata 同时 in-flight chat
  在 mutate — 读路径阻塞

RWLock 让多个 reader 并发读 session state；mutating operations 独占。

## 设计

`SessionRWLock` 用 `asyncio.Condition` + reader counter + writer flag：

- `read()`: 多个 reader 并发。writer 等待时新 reader 阻塞。
- `write()`: 独占；等待 reader + 当前 writer 完成。writer 优先防 starvation。
- 兼容 `SessionLockManager.run()` 的 (session_id, producer) 模式 —
  `SessionRWLock.run_with_write_lock(session_id, producer)`。
- Drop-in 替代：保留现有 `SessionLockManager` API，新 manager
  `SessionReadWriteLockManager` 提供 `acquire_read()` / `acquire_write()`。

## Files

1. `tradingagents/agent_harness/core/session_rwlock.py` (新) — async RWLock primitive
2. `tradingagents/agent_harness/core/session_rwlock_manager.py` (新) — per-session 管理器
3. `web/app.py` — 把 `app.state.session_lock` 升级为新 manager，旧
   `run()` API 仍可用
4. `tests/test_step39_session_rwlock.py` — 8 测试
