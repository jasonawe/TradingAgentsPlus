# Harness Architecture Review — 2026-09-18

## Scope
Periodic audit of `tradingagents/agent_harness` covering
高可用 / 高扩展 / 高可靠 三条目标线,以 Step 1-19 完成后的
当前状态为准。

## 1. 高扩展 (Extensibility)

| 维度 | 现状 | 评估 |
|---|---|---|
| Tool 注册 | `ToolRegistry.register()` + `install_builtin_tools()` | ✅ 单点注册,33 个工具即插即用 |
| Intent 路由 | `extract_slots` + `classify` + `_CRUD_DISPATCH` 三层 | ✅ Slot-aware 优先,entity × op 次之,legacy 兜底 |
| Multi-intent | `_run_multi` 串行调度 + `_summarise_multi` 聚合 | ✅ Step 5 完成后,1 个 SSE turn 跑多个 read |
| 短期扩展点 | symbol-less + report_id slot (Step 17/18) | ✅ run-* / report-* 都支持 |
| **缺口** | D2 Tool 重构 (v2 spec §D2) | ⚠️ Tool 当前还是 Pydantic + 自定义 wrapper,L2/L3 grading 缺乏统一接口 |
| **缺口** | D3 Workflow 独立 (v2 spec §D3) | ⚠️ state machine 在 orchestrator 里硬编码,workflow 不可声明式 |

## 2. 高可用 (Availability)

| 维度 | 现状 | 评估 |
|---|---|---|
| Crash recovery | `HarnessCheckpointStore` + `/api/harness/chat/resume` | ✅ 崩溃后可 resume |
| Session lock | `SessionLockManager` (per-session lock) | ✅ 同 session 并发拦截 |
| Token accounting | `TokenUsageStore` (disjoint by agent) | ✅ Step 12+ cache totals |
| Multi-session | Step 19 三端点 + UI 切换器 | ✅ 用户可手动多 session |
| **缺口** | Session 自动恢复点 (resumable checkpoint 粒度) | ⚠️ checkpoint 现在是完整 turn,无法 partial replay |
| **缺口** | Plan cache 跨进程 (in-memory only) | ⚠️ 重启后 Tier 2 plan 重生成,plan_cache 缓存污染仍可能 |

## 3. 高可靠 (Reliability)

| 维度 | 现状 | 评估 |
|---|---|---|
| Verification | L1 schema / L2 missing / L3 grounded | ✅ Step 12 audit persist |
| HITL gate | `_check_write_approval` + friendly impact_note | ✅ Step 9+10 完成 |
| Audit log | `impact_note` + `reason_short` 持久化 | ✅ Step 9+10+12 完成 |
| Cache stats | `cache_stats` SSE 事件 + UsageBucket | ✅ Step 11 完成 |
| **缺口** | Tier 2 plan args 缓存污染 | ⚠️ Step 18 已部分修(reduce 缓存命中 bad args) |
| **缺口** | LLM 误判 → 必须调用工具而非幻觉 | ⚠️ Step 16+17/18 缩短了 routing 路径,但 LLM 仍可能在长 context 中编造数字 |

## 4. 用户痛点 → 设计 gap

| 痛点 | 设计 gap | 修法 |
|---|---|---|
| 同一会话后,用户说"这个资产"不知道指什么 | `slots` 当前只跨单次请求,不会自动 inherit 到下一 turn | **下一步**:SessionState.slots accumulator + 上一 turn 的 symbols 进 active set |
| 多 session 切换后旧 session 上下文丢失 | L3 references 层已设计但未接入 | **下一步**:L3 step — 跨 session symbol 标签 + reference 路由 |
| "深度分析" LLM 经常编造数字 | LLM 在 synthesize 阶段可以忽略 tool data | **下一步**:synthesizer 强制要求数据引用栏,未引用扣分 |
| 跨 session 想继续上次话题 | L1 滚动归档已实现,但无 resume cross-session | **下一步**:`/api/harness/sessions/<id>/fork` 接口 |

## 5. 推荐下一步 (按优先级)

1. **Session slot carry-forward** (Step 20, P0)
   - 把上一 turn 的 `slots['active_symbol']` 注入下一 turn 的 slot pool
   - 解决"这个资产"上下文断裂

2. **L3 references layer 接通** (Step 21, P0)
   - `MemoryManager.l3.get_relevant(message)` 在 LLM synthesize 前调
   - 让 LLM 看到"上次讨论了 AAPL 的 X / Y,继续吗?"

3. **Synthesizer 引用栏强制** (Step 22, P1)
   - LLM 输出 markdown 必须包含 `> 来源: get_quote / get_news / ...`
   - 缺失则 L3 judge 扣分,失败重试

4. **D2 Tool 重构** (Step 23, P1)
   - 抽象 `Tool` ABC:统一 args/result/permission/lifecycle 四个面
   - 现有 33 个工具迁移后,新工具开发时间 -50%

5. **Plan cache 跨进程** (Step 24, P2)
   - 把 in-memory plan_cache 持久化到 web_runs.sqlite
   - 重启后 plan 不再重生成

## 6. 复盘结论

**整体已达 P3+P4+P5+P6+P7 stage,33 个工具 0 LLM short-circuit 路径
跑通 (Step 16-18)。多 session UI 已上线 (Step 19)。**

**但用户体验上仍有 4 个明显 gap**(参见 §4),优先级最高的是
slot carry-forward(同一 session 多轮上下文的"这个资产"问题)。

— 当前完成度 ~ 70%(以 v2 spec 为 100% 计)。
— 已覆盖: T1/T2/T3 tier, multi-intent, write HITL, audit, 多 session。
— 未覆盖: D2 Tool 重构, D3 Workflow 独立, D6 Verification 三层,
 跨 session memory 接通 (L3)。
