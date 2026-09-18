# QuoteService Cache Invalidation Hook — 2026-09-18 (Step 40)

## Background

v3 review backlog item C. 当前 QuoteService 是 TTL-based cache (15–60s)，
没有 explicit invalidation 入口：

- 用户手动刷新行情 → 仍走 TTL 等待
- Post-earnings announcement → 旧数据延迟展示
- Watchlist 标的手动标记 "stale" → 无法立即重 fetch
- Provider 报告 error 数据修正 → 旧 cache 持续返回错值

## 设计

`QuoteService.invalidate(symbol=None, asset_type=None)`:

- `invalidate(symbol, asset_type)` — 删特定 (symbol, asset_type) cache row + inflight event
- `invalidate(asset_type=...)` — 清某 asset class 全部 cache
- `invalidate()` — 全清（nuclear option）
- `purge_stale(older_than_seconds)` — 按 fetched_at 清理 stale rows

Hook 集成：
- `web/api/...` POST `/api/market/invalidate` — 管理员按钮
- `watchlist_items` 表注 `invalidated_at` 列时 market_state.py 自动调用
- LLM announce "earnings released" 时 tool call `invalidate_quote_cache(symbol)`

## 文件

1. `web/market_data.py` — 加 `invalidate(symbol, asset_type, all_)` /
   `purge_stale(older_than_seconds)` / 维护 `_last_invalidation` event log
3. `web/app.py` — `POST /api/market/invalidate` endpoint
5. `tests/test_step40_quote_invalidation.py` — 8 测试
