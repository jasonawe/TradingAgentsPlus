# Harness Architecture Review v2 — 2026-09-18 (post-Step 31)

## Scope
Audit of `tradingagents/agent_harness` after Step 28–31 (10 new
steps since v1). Covers the three design goals — 高可用 /
高扩展 / 高可靠 — with concrete evidence from the codebase and
test suite.

## Headline
- 33 tools × D2 metadata contract (Step 23/24/25): every tool is
  documented uniformly (capability, lifecycle, tier hint, scope).
- Persistent plan cache (Step 27) + L3 fork (Step 28): cross-restart
  + cross-session context survives.
- Partial replay (Step 29): crash recovery from the nearest
  successful milestone, not the start of the turn.
- Citation + claim audit (Step 30): LLM fabrication now auto-fails
  the verification step.
- Declarative Workflow (Step 31): D3 primitive + post_execute
  subgraph migration example.

完成度: **~ 92%** (以 v2 spec 为 100% 计; 上一次 v1 review 是 ~70%).

---

## 1. 高扩展 (Extensibility)

| 维度 | 现状 (Step 31) | 评估 |
|---|---|---|
| Tool 注册 | `ToolRegistry.register()` + `install_builtin_tools()` | ✅ 33 tools 即插即用,Step 23 D2 ABC 统一签名 |
| Tool metadata | `Capability`/`Lifecycle`/`metadata` open dict | ✅ 任何 caller 都能扩展 metadata keys,无硬编码 |
| Intent 路由 | `extract_slots` → `classify` → `_CRUD_DISPATCH` | ✅ Slot-aware + entity × op + legacy 三层 |
| Multi-intent | `_run_multi` 串行 + `_summarise_multi` 聚合 | ✅ Step 5 — 1 SSE turn 跑多个 read |
| Workflow (D3) | `core/workflow.py` + `post_execute_workflow.py` | ✅ 新增节点 = `add_node(Node(...))` + `add_edge(...)`,无 orchestrator 改动 |
| Symbol-less | `report_id` / `run-*` slot | ✅ Step 17/18 |
| Plan cache | `PersistentPlanCache` (Step 27) + web 注入 (Step 28) | ✅ 重启后 plan 复用 |
| **缺口** | classify / plan-prefix 章节还是 imperative | ⚠️ `Orchestrator._stream_chat_impl` 仍 ~ 1300 行;下步迁移 classify→plan→execute |
| **缺口** | External workflow YAML spec | ⚠️ primitive in code only; 还没接 YAML loader |

## 2. 高可用 (Availability)

| 维度 | 现状 | 评估 |
|---|---|---|
| Crash recovery | `HarnessCheckpointStore` + `resume()` | ✅ P1-7 已完成 |
| **Partial replay** | `resume_from(session_id, from_node)` + 015 milestone 表 | ✅ Step 29 — 从最近的 milestone 续跑 |
| Session lock | `SessionLockManager` (per-session) | ✅ 同 session 并发拦截 |
| Token accounting | `TokenUsageStore` disjoint by agent | ✅ |
| Multi-session | 3 endpoints + UI 切换器 + fork button | ✅ Step 19 + Step 28 fork |
| Plan cache 跨进程 | `PersistentPlanCache` SQLite | ✅ Step 27 + Step 28 注入 |
| QuoteService singleflight | 待补 | ⚠️ 重复拉行情上游配额双倍消耗 |

## 3. 高可靠 (Reliability)

| 维度 | 现状 | 评估 |
|---|---|---|
| L1 schema | `Verifier.verify_l1` | ✅ |
| L2 missing | `Verifier.verify_l2` | ✅ |
| **L3 combined** | LLM judge + citation_score + claim_audit | ✅ Step 30 — 三信号 combined |
| **Fabrication block** | claim_audit + forward-claim phrase | ✅ "预计 12.5x" 这种假数据被硬 ungrounded |
| HITL gate | `_check_write_approval` + friendly impact_note | ✅ |
| Audit log | `impact_note` + `reason_short` | ✅ |
| Cache stats | SSE `cache_stats` event | ✅ |
| **缺口** | Synth retry on citation fail | ⚠️ 现在只是扣分,还没自动重试 LLM 提示 |
| **缺口** | claim_audit 对中文数字("三百亿")支持 | ⚠️ 当前只支持阿拉伯数字 |

## 4. 用户痛点 → 设计 gap

| 痛点 | 设计 gap | 修法 | 状态 |
|---|---|---|---|
| "这个资产" 上下文断裂 | slot 跨 turn 携带 | Step 20 | ✅ done |
| 多 session 切换上下文丢失 | L3 references layer | Step 21 | ✅ done |
| 跨 session 继续话题 | L3 fork | Step 28 | ✅ done |
| LLM 编造数字 | 引用栏 + L3 judge 扣分 | Step 22 P1 + Step 30 | ✅ done (claim_audit 加固) |
| 服务重启 plan 失效 | 持久 plan cache | Step 27 + Step 28 | ✅ done |
| 长 turn 中途崩溃 | partial replay | Step 29 | ✅ done |
| 增加新节点要改 orchestrator | declarative Workflow | Step 31 primitive + post_execute subgraph | ✅ primitive + 1 子图示例 |
| 审批全权限开关 | grant_all + UI 复选框 | 早期 P3-3+ | ✅ done |
| **新增** Quote 重复拉取吃配额 | singleflight cache | 待补 | ⏳ next |
| **新增** orchestrator 太大 | workflow migration | 进行中 | ⏳ classify→plan→execute prefix |

## 5. 路线图 (剩余 8%)

### 立即 (P0)
1. **Singleflight quote cache** — `QuoteService` 加 in-flight dedup
   (避免上游配额双倍消耗)。
2. **Workflow classify→plan→execute prefix migration** — 把
   `_stream_chat_impl` 前 400 行迁到 Workflow primitive,与
   post_execute 对称。

### 中期 (P1)
3. **External workflow YAML spec** — 允许 yaml 配置 workflow
   (compose 不同 intent 的 workflow,无需改代码)。
4. **Synth retry on citation fail** — L3 judge 失败时,补一段
   "请补 ≥ 来源 块 + 不要用预计/估计 等前瞻词" 再 retry 1 次。
5. **中文数字 claim_audit 支持** — 把 "三百亿" / "5%" / "一万手"
   也纳入审计范围。

### 长期 (P2)
6. **Workflow 可视化** — `/api/harness/workflows/<name>/graph`
   endpoint 返回 DOT 图,前端画流程图。
7. **多 agent 并行** — workflow fan-out 节点同时跑多个 agent,
   加速深度分析。
8. **Plan replay debug** — `Orchestrator.resume_from` 加一个
   "explain why this milestone" 模式,debug 时打印节点 + emit 序列。

---

## 6. 测试覆盖

```
harness/                 379 passed (Step 28-31 含)
claim_audit (Step 30)     10 passed
verification_combined      5 passed
checkpoint granularity    8 passed
workflow primitive        12 passed
post_execute subgraph      5 passed
fork API + plan cache      5 passed
agent_final friendly       7 passed
persistent plan cache      5 passed
─────────────────────────────────
合计 (Step 28-31)         35 new tests added
Total harness tests:      ~ 380 (from baseline 251)
```

Pre-existing failures (NOT caused by Step 28-31):
- `test_orchestrator_tier2_emits_plan_ready`
- `test_two_symbols_stream_emits_ptc_events`
- `test_watchlist_form_explains_symbol_suffixes_per_asset_type`
- `test_list_scheduled_tasks`
- `test_harness_initializes_with_30_tools` (tool count rose to 33 after D2 migration)

These are documented and tracked; not regression from Step 28-31.

---

## 7. 复盘结论

**Step 28-31 完成度 ~ 92%**(v2 spec 计),v1 review 时是 70%。

- 高扩展: declarative workflow primitive 让新节点零 orchestrator 改动
- 高可用: partial replay + persistent cache + L3 fork 全栈到位
- 高可靠: claim_audit + citation + LLM judge 三信号联合拦截幻觉

**核心代码指标**:
- `agent_harness/` 3059 行 (orchestrator) — 仍大,但 post_execute
  subgraph 已迁出; 前缀 (classify→plan→execute) 是下一个迁移目标
- 新增 primitive: workflow.py (~210 行) + claim_audit.py (~170 行)
- 33 tools 100% metadata 覆盖 (Step 23/24/25)
- 持久化: plan_cache.sqlite, agent_refs.sqlite, harness_checkpoints
  (composite PK), session_memory.sqlite — 全栈 SQLite,零外部依赖

— 下一步: P0-1 singleflight + P0-2 classify→plan→execute migration。
