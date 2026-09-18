# HITL Approval Audit Race Fix — 2026-09-18

## Background

User 反馈：审批 dialog 不出现、audit 表与 tool 执行 status 不一致。
v3 review 里 backlog item B。

## 已存在的 race fix（保持）

- `claim_write_audit` (audit.py) 原子 CAS: `pending → confirmed/rejected`
- Confirm endpoint (web/app.py `_harness_confirm`) loser 不重 invoke tool

## 剩余 race condition

| ID | 描述 | 影响 |
|---|---|---|
| R-E | `_approved` set 是 in-memory；进程重启后 audit row 是 confirmed，但 `is_approved()` = False → tool 重新 gate → 用户必须再次确认 | UX 阻塞 + audit 重复行 |
| R-G | audit row 在 pending/confirmed 永远不被消费（LLM 决定不再调 tool / tool invoke 超时被打断） | 表无限增长 + audit viewer 误导 |
| R-F | LLM parallel tool calls 在同一 turn 多个写工具 → 多个 audit rows。前端 UI 显示一个 dialog，但另外的 audit row 用户感知不到 | 用户可能未意识到第二个工具也需确认 |
| R-H | Tool invoke 失败 → `update_write_status("failed")` best-effort，DB 不可用时 audit row 留在 confirmed 状态（看着像成功） | audit viewer 显示 "执行成功" 但实际工具抛错 |

## Fix 设计

### Fix 1: Persistent approval registry

`_approved` 改为 SQLite-backed。`_key_of` (tool_name, args_json) →
`tool_approvals` 表：

```sql
CREATE TABLE tool_approvals (
    session_id TEXT,
    tool_name TEXT,
    args_json TEXT,
    created_at TIMESTAMP,
    consumed_at TIMESTAMP,  -- NULL = pending
    audit_id INTEGER,
    PRIMARY KEY (session_id, tool_name, args_json)
);
```

`is_approved()` / `grant_approval()` / `consume_approval()` 走 SQLite
而不是 in-memory set。in-memory fast-path 仍可用作 L1 cache，TTL ≤
session lifetime。

### Fix 2: Audit row sweeper (housekeeping)

`tradingagents/agent_harness/audit_sweeper.py`：

- `expire_stale_pending(min_age_seconds=600)` — pending 状态超过 10 分钟 → expired
- `expire_stale_confirmed(min_age_seconds=600)` — confirmed 状态超过 10 分钟但 audit row 没有对应 executed/failed → expired
- `retry_failed_audit_updates()` — collect in-memory `_failed_audit_log` list，retry `update_write_status` with backoff

Housekeeping 可以由 web app 启动时跑一次（warmup），或由 schedule 任务定期跑。

### Fix 3: Multi-gate batch confirmation

Orchestrator 在 emit `confirm_request` event 之前检测同一 turn 内的
多个 pending audit rows — emit `batch_confirm_request` event 包含所有
audit_ids。前端一次性渲染多 gate dialog。

向后兼容 — 当只有 1 个 audit row 时，emit `confirm_request`（单数）
不变；>=2 时 emit `batch_confirm_request`。

### Fix 4: Tool invoke status barrier

Confirm endpoint `_harness_confirm` 把 tool invoke 包在 try/except/
finally 中，failed audit update 在 DB 不可用时入队 `_failed_audit_log`
list（in-memory + SQLite），housekeeping 任务重试。

## 文件改动

1. `tradingagents/agent_harness/hitl.py` — 新增 SQLite-backed
   approval store，保留 in-memory fast path
3. `tradingagents/agent_harness/audit_sweeper.py` (新) — orphan row
   cleanup + failed update retry
4. `tradingagents/agent_harness/audit.py` — 增加
   `expire_audit_row` helper
5. `web/app.py` — confirm endpoint 用新的 SQLite approval，
   collect failed updates 到 sweeper queue
7. `tradingagents/agent_harness/core/orchestrator.py` —
   `_stream_chat_impl` 在 emit confirm_request 之前聚合多 gate
8. `web/migrations/016_tool_approvals.sql` (新) — schema
9. `tests/test_step38_hitl_race.py` (新) — 13 测试覆盖 4 个 fix

## 完成度信号

- 现有 race 测试 (`test_audit_race_fix.py`) 全通过
- 新增 13 测试覆盖 SQLite approval persistence / sweeper /
  batch confirm / failed update retry
- 重启 service 后，audit 表里 orphan rows 自动清理
