# Harness 改进路线图 — 高可用 / 高扩展 / 高可靠

> 日期: 2026-09-14
> 上下文: 调研完 deepseek-harness 21 篇文档 + 自检我们的 59 文件 5232 行 harness 后
> 目的: 把散落在 `2026-09-14-deepseek-harness-architecture-study.md`(1551 行,§1-14)里的洞察,**按目标维度重新组织**,给可执行的改进路线
> 受众: 我自己(实施时回看)+ 未来 review 时快速定位
> 不重复: 详细原理 + dsh 原文引用见 study doc;本文件只讲**做什么 / 为什么 / 多大工作量**

---

## 摘要(30 秒读完)

我们的 harness 在**高扩展**上 60% 已达(tools / plugins / LLM providers 都注册化),**高可用** 30%(无 Session 类、无 in-flight 锁、无 crash recovery),**高可靠** 40%(L1 history 静默截断、无审计 trail、LLM 单次 complete 无流式)。

按 ROI 排序的 P0(1 周,3.5 天)集中在**高可用 + 高可靠**两个杠杆点上:

| 优先级 | 动作 | 维度 | 工作量 | 价值 |
|---|---|---|---|---|
| P0-1 | `quote_provider` seam | 扩展 | 0.5d | 高(用户上轮诉求)|
| P0-2 | PTC 模式(LLM 写 async program)| 可靠 | 1.5d | **极高**(L3 多工具并发)|
| P0-3 | Tool 管线 5 阶段 | 可用 + 可靠 | 1d | **极高**(HITL + L3 fallback)|
| P0-4 | Surface event 分类 + L1 append-only | 可靠 | 1d | **极高**(审计 + 回放)|

完整 P0 + P1 + P2 ≈ **25 天**(见 §4 路线图)。

---

## 核心原则(避免过度设计)

dsh 的 5232 行是我们能用的天花板,但**我们不该搬**:

1. **Cordis 插件树** — Python 生态差异,dataclass + register 已够
2. **Branded IDs / merge-extensible map / `ignorable?: true`** — TS 特性,Python 不需要
3. **Append-only 全量历史** — 没跨 session resume 需求,只搬"分类"不搬"全量持久化"
4. **Multi-persistence backend(JSONL/SQLite)** — 单 SQLite 够
5. **Profile + Bundle 分层组合** — 单 web app 用不到
6. **Worker-thread PTC backend** — 我们 web 端无沙箱
7. **ACP / SDK / 多 profile CLI** — 单一 web app
8. **完整 bracketed `AssistantStreamFrame`** — SSE 推送用 dict 就够

**只搬心智模型,不搬代码**:
- Capability seam 三段式(定义/实现/消费者)
- Tool 管线 5 阶段(pre / guards / execute / post / result)
- Event 三模式(emit / waterfall / around)
- Surface event 分类(surface / log-only)
- Append-only + Surface projection(只搬分类 + 替换语义,不搬全量)

---

## 一、高可用(Availability)— 服务不挂、挂了能恢复

### 1.1 我们当前的问题

| # | 问题 | 现状 | 影响 |
|---|---|---|---|
| 1.1.1 | **In-flight 无锁** | 同一 `session_id` 并发 `stream_chat` 会竞态写同一个 db | history 损坏 / tool 执行顺序错乱 |
| 1.1.2 | **两套 session 存储不对齐** | LangGraph SqliteSaver(`data_dir/sessions/agent_*.db`)+ agent_harness L1(`.ta_cache/session_memory.sqlite`)| 同一 session 在两处写,**内容不一致** |
| 1.1.3 | **list_sessions 半成品** | `web/app.py:1601` 硬编码 `last_active: None` | 前端永远看不到真实活跃时间 |
| 1.1.4 | **无 Session 删除接口** | `DELETE /api/agent/sessions/{id}` 不存在 | db 文件孤儿,无清理 |
| 1.1.5 | **无 crash recovery** | 流式断连 / 服务挂 → in-flight turn 丢失 | 用户体感"刚才说到一半没了" |
| 1.1.6 | **dangerous tool 无安全闸** | `delete_alert` / `cancel_schedule` / `drop watchlist` 跟 `get_quote` 同权限 | HITL 靠 `permission` enum,**绕过 = 直接执行** |

### 1.2 dsh 的对应设计

- **`AgentStatus = 'idle' | 'running'`** — engine 内追踪,in-flight 唯一
- **Cancel convergence wake latch** — cancellation + wakeup 协调
- **single-source Session log** — 一份 log 一个 schema,no 重复
- **`session/end-seed` marker** — crash recovery 区分"crash 时正在跑的 X"vs"刚加载的 X"
- **`assistant/message.interrupted` marker** — crash recovery 合成
- **Tool 管线 monotonic guards** — 终态拒绝,后置监听器无法覆盖
- **`tools/pre-execute` waterfall** — `return 'ask'` → 走 `ctx.approval` 审批

### 1.3 推荐动作

| # | 动作 | 工作量 | 价值 | 来源 |
|---|---|---|---|---|
| **A1** | **In-flight 锁**(`session_active: dict[str, asyncio.Lock]`)| 0.5d | **极高**(防 race) | dsh AgentStatus |
| **A2** | **Session 元数据表** `sessions(id, created_at, last_active, user_id, message_count, status)` | 0.5d | 高(查询 + 管理)| dsh Session class |
| **A3** | **DELETE /api/agent/sessions/{id}** + 级联清理 db 文件 + audit | 0.5d | 中-高(治理) | 我们自身缺口 |
| **A4** | **Crash recovery**(`assistant/message.interrupted` marker + restart 检测)| 1d | 高(可靠性) | dsh end-seed |
| **A5** | **Tool 管线 5 阶段 + monotonic guards**(P0-3 包含)| (1d) | **极高** | dsh tool pipeline |
| **A6** | **合并两套 session 存储** — agent_harness L1 改为 LangGraph checkpointer 单源 | 1d | 高(避免状态分裂)| 我们自身缺口 |

**P0 子集(A1 + A5):1.5 天,极高价值**

### 1.4 不要搬的

- ❌ 完整 SessionEvent append-only log 持久化(我们用 SQLite 已够)
- ❌ `session/end-seed` marker 的 fork 语义(我们没 fork 需求)
- ❌ Crash recovery 的 inbox delivery latch(我们的 web 流式不需要)

---

## 二、高扩展(Extensibility)— 加新能力不动 framework

### 2.1 我们当前做得好的(5 个)

| # | 我们已有的 | 为什么好 |
|---|---|---|
| 2.1.1 | **ToolRegistry** 装饰器 + 手动 + entry_points | Python 生态标准做法 |
| 2.1.2 | **PluginRegistry** `discover_entry_points(group='agent_harness.plugins')` | 第三方 plugin 可独立 pip install |
| 2.1.3 | **LLMProvider** ABC + `LLM_REGISTRY` factory + 5 native providers | 加新 LLM = 注册 factory |
| 2.1.4 | **AgentRegistry** 注册式 6 个 builtin agents | 加新 agent = 写一个 `BaseAgent` 子类 |
| 2.1.5 | **Prompt 分发到 plugin**(`AlertPlugin.prompts()`/`QuantPlugin.prompts()`)| 每个 plugin 自带 prompt |

### 2.2 我们做得不够的(6 个)

| # | 缺口 | 现状 | 影响 |
|---|---|---|---|
| 2.2.1 | **Provider 没 seam** — 数据源绑死 eastmoney | `tools_bridge.py` 直接 `EastMoneyQuoteProvider()` | 用户上轮问"yfinance/eastmoney 可切换" 答不出 |
| 2.2.2 | **Sub-agent 写死** — `data/news/alpha/synthesizer` 是内部函数 | `agents/data_agent.py` / `alpha_agent.py` 写死 `def run()` | 加 qstock/akshare 要改 framework |
| 2.2.3 | **无 Event 三模式抽象** — 散落 `retry_async` / `circuit_breaker` / `audit.log` | 没有统一的 `emit/waterfall/around` 注册点 | 第三方插件挂不上 cross-cutting |
| 2.2.4 | **System prompt 字符串拼接** — `ContextPriority` 8 层固定 | 不支持 section 化 / `{{var}}` / scope shadow | 加 plugin prompt 要改 ContextPriority |
| 2.2.5 | **Per-agent 无 scope 隔离** — sub-agent 共享主 agent scope | `ToolContext(session_id=...)` 单字段 | sub-agent 工具可能污染主 agent |
| 2.2.6 | **前端 SSE 写死** — `(event, payload)` 单通道 | 新 event 类型要改前端 framework | 加新 event 要重新部署前端 |

### 2.3 dsh 的对应设计

- **Capability Seam 三段式**(definition / provider / consumer)— 72 个 seam
- **SubagentProvider** 注册表 + 6 backend(spawn/fork-in-process / ACP / Codex / Claude Code / DSH SDK)
- **Event 三模式**(`emit` / `waterfall` / `around`)
- **SystemPrompt.section / .context / .variable / .tools** — 任意 section 注册,`{{var}}` 插值,scope shadow
- **Scope 三件套**(`createScope` / `scopeOf` / `scopeTarget`)+ per-agent `restrict_tools`
- **ConversationNodeAssembler** Definition 注册制 — 加新 target 不改 framework

### 2.4 推荐动作

| # | 动作 | 工作量 | 价值 | 来源 |
|---|---|---|---|---|
| **E1** | **`quote_provider` seam** — 4 个 provider(eastmoney/yfinance/akshare/stub)注册表 | 0.5d | 高(用户上轮)| dsh capability seam |
| **E2** | **`news_provider` seam** — 4 个(stub/newsapi/akshare/rss)| 0.5d | 高(对应 E1) | dsh capability seam |
| **E3** | **`alpha_provider` seam** — qstock / akshare / qlib stub | 1d | 中-高 | dsh capability seam |
| **E4** | **Section / Context 二元** + `{{var}}` 插值(`ContextPriority` 升级)| 1d | 高(表达力)| dsh systemPrompt |
| **E5** | **Subagent named provider**(`data_agent` / `news_agent` / `alpha_agent` / `synthesizer` 注册化)| 1d | 中(扩展) | dsh subagent |
| **E6** | **Event 三模式抽象** — `ctx.emit / ctx.waterfall / ctx.around` | 1d | 高(插件化)| dsh event |
| **E7** | **Per-agent scope** — sub-agent 独立 scope,tool restriction | 1d | 中(隔离)| dsh scope |
| **E8** | **前端 SSE Definition 注册** — `ConversationNodeDefinition` 模式 | 1d | 中(前端可扩展)| dsh conversation |

**P0 子集(E1 + E4):1.5 天**

### 2.5 不要搬的

- ❌ Capability Seam 的 72 个全分类(我们用 5-6 个够)
- ❌ Subagent 6 个 backend(spawn/fork/acp/codex/claude-code/dsh-sdk)— 我们 web 端只 1 个 in-process
- ❌ Scope 的 `scopeTarget` / `scopeOf` 三件套全套(我们 scope 简化版够)
- ❌ ConversationNodeDefinition 的 6 件套全套(`buildLocationData` / `buildViewNode` 我们不需要)

---

## 三、高可靠(Reliability)— 不会错、错了能查

### 3.1 我们当前的问题

| # | 问题 | 现状 | 影响 |
|---|---|---|---|
| 3.1.1 | **L1 history 静默截断 200 条** | `append_message()` `msgs = msgs[-200:]` | 长会话上下文丢失,LLM 看不到早期对话 |
| 3.1.2 | **无审计 trail** | `audit.log()` 单点写,不可回放 | 出错不知哪一步 |
| 3.1.3 | **System prompt 字符串拼接** | `ContextPriority.assemble()` 拼字符串 | system prompt 改动无 audit |
| 3.1.4 | **LLM 单次 complete,无流式** | `complete(messages) -> LLMResponse` | 长生成用户看不到进度 / 无 incremental 计费 |
| 3.1.5 | **TokenUsage `dict[str, int]`** | 随便塞 | cache hit / reasoning token 算不准 |
| 3.1.6 | **LlmFailure 不归一** | 5 个 provider 各自抛不同异常 | retry 决策不可靠 |
| 3.1.7 | **`RetryPolicy` 不知错误类型** | `max_retries=2`,一刀切 | provider 限流 / 配额用完时瞎重试 |
| 3.1.8 | **AppIdentity(User-Agent)无** | 默认 SDK UA | provider 端追踪不到 |

### 3.2 dsh 的对应设计

- **Append-only SessionEvent log** — 不可覆盖,完整历史
- **Surface event 分类**(4 surface + N log-only)— 审计清楚
- **`system/message` 入 derived history** — system prompt 跟 user/assistant/tool 一样是 surface event,可回放可 replace
- **StreamChunk 7 类型** + **closed discriminated union** + **BlockAssembler** 单实现
- **TokenUsage disjoint** 6 字段(input/output/cacheRead/cacheWrite/reasoning/total)
- **LlmFailure 归一**(`message/code/statusCode/providerRetryAfterMs`)
- **ResolvedRetryPolicy** `{mode, maxRetries, retryableCodes, initialDelayMs, maxDelayMs, jitterRatio}`
- **AppIdentity** 强制 User-Agent

### 3.3 推荐动作

| # | 动作 | 工作量 | 价值 | 来源 |
|---|---|---|---|---|
| **R1** | **L1 append-only event log** — `Event{seq, type, data, time}`,LLM 上下文从 log 派生 | 1d | **极高**(审计 + 回放 + replace)| dsh session log + surface |
| **R2** | **Surface event 分类** — 4 surface(system/user/assistant/tool-result)+ N log-only | 0.5d | **极高**(前端简化 + 审计)| dsh surface |
| **R3** | **`system/message` 入 derived history** — system prompt commit as event | 0.5d | 高(可 replace)| dsh system-as-event |
| **R4** | **LLM 流式协议 + 7 条 adapter 契约** | 1d | **极高**(用户问"深度分析"边生成边推)| dsh streaming |
| **R5** | **TokenUsage disjoint 6 字段** | 0.5d | 高(cache/reasoning 精确)| dsh TokenUsage |
| **R6** | **LlmFailure 归一**(`code` / `statusCode` / `providerRetryAfterMs`)| 0.5d | 高(retry 决策)| dsh LlmFailure |
| **R7** | **ResolvedRetryPolicy retryableCodes** | 0.5d | 中-高(provider 限流精准)| dsh ResolvedRetryPolicy |
| **R8** | **AppIdentity User-Agent 强制** | 0.5d | 低(provider 友好)| dsh AppIdentity |
| **R9** | **L1 history 滚动归档** — 改成压缩 / 滚动,不静默丢 | 0.5d | 中(长会话)| 我们自身缺口 |

**P0 子集(R1 + R2 + R3 + R4):3 天,极高价值**

### 3.4 不要搬的

- ❌ 完整 7 种 SurfaceEventType 全套(我们用 `system/message` + `user/message` + `assistant/message` + `tool/result` 4 类够)
- ❌ `ReplayEnvelope` opaque state(我们没跨 session resume)
- ❌ `expandAssistantStream` 严格 record 校验(我们不持久化 raw stream)
- ❌ `assistantStreamFirstTokenTime` 等 8 个 record-level reader(我们不需要)

---

## 四、30 天路线图(按 ROI 排序)

### 4.1 P0 — 1 周内(3.5 天,极高价值)

| Day | 动作 | 维度 | 来源 |
|---|---|---|---|
| **D1 上午** | **E1** `quote_provider` seam | 扩展 | §11.7 |
| **D1 下午** | **R2** Surface event 分类 | 可靠 | §14.4 A4 |
| **D2** | **E4** Section / Context 二元 + `{{var}}` | 扩展 | §13.9 P0-S2/S3 |
| **D3-D4 上午** | **PTC 模式骨架**(`PTCExecutor`,asyncio.gather 并发)| 可靠 | §11.7 + §11.3 B |
| **D4 下午 - D5** | **R3 + R4 部分** `system/message` as event + LLM 流式骨架 | 可靠 | §13.9 + §14.4 A1 |

**P0 完成后**:
- ✅ 用户能切换 eastmoney ↔ yfinance ↔ akshare
- ✅ LLM 可并发调 quote+fundamentals+news,不再"unknown tool ''"
- ✅ system prompt 进审计,不再字符串拼接
- ✅ 现有功能完全兼容

### 4.2 P1 — 月内(2 周,~10 天)

| 周 | 动作 |
|---|---|
| **W2-D1** | **A1** In-flight 锁(0.5d)|
| **W2-D1 下午** | **A5** Tool 管线 5 阶段 + monotonic guards(1d)|
| **W2-D2** | **R1** L1 append-only event log(1d)|
| **W2-D3** | **A2** Session 元数据表(0.5d)|
| **W2-D3 下午** | **A3** DELETE /sessions 路由(0.5d)|
| **W2-D4** | **R5 + R6** TokenUsage disjoint + LlmFailure 归一(1d)|
| **W2-D5** | **R7 + R8** ResolvedRetryPolicy + AppIdentity(1d)|
| **W3-D1** | **E5** Subagent named provider(1d)|
| **W3-D2** | **E2** `news_provider` seam(0.5d)|
| **W3-D2 下午** | **E3** `alpha_provider` seam(0.5d)|
| **W3-D3** | **E6** Event 三模式抽象(1d)|
| **W3-D4** | **E7** Per-agent scope(1d)|
| **W3-D5** | **A4** Crash recovery(1d)|

**P1 完成后**:
- ✅ 同一 session_id 并发安全
- ✅ Tool 管线完整(权限闸 + L3 fallback + 审计)
- ✅ LLM 流式 + 精确计费 + 错误归一
- ✅ Sub-agent 可热插拔 + 隔离
- ✅ 服务挂了能恢复

### 4.3 P2 — 季度(看需求,~11 天)

| # | 动作 | 维度 | 来源 |
|---|---|---|---|
| **P2-1** | **E8** 前端 SSE Definition 注册(1d) | 扩展 | §14.4 A4 |
| **P2-2** | **A6** 合并两套 session 存储(1d) | 可用 | §1.1.2 |
| **P2-3** | **R9** L1 history 滚动归档(0.5d) | 可靠 | §3.1.1 |
| **P2-4** | **Q1** Turn/Step boundary + TurnEndReason(0.5d) | 可靠 | §12.6 P0-M3 |
| **P2-5** | **Q2** Session 列表 + 跨 session 查询(0.5d) | 扩展 | §12.6 P1-M1 |
| **P2-6** | **Q3** Fork + session/end-seed marker(1d) | 扩展 | §12.6 P1-M2 |
| **P2-7** | **Q4** Agent steer / inject / whenIdle(0.5d) | 扩展 | §12.6 P1-M3 |
| **P2-8** | **Q5** Cooperative waterfall for systemPrompt(1d) | 扩展 | §13.9 P1-S2 |
| **P2-9** | **Q6** Lifecycle hook bridges(1d) | 可用 | §11.4 P2-1 |
| **P2-10** | **Q7** SurfaceOp.replace 模型完整迁移(1d) | 可靠 | §11.4 P2-2 |
| **P2-11** | **Q8** Profile + Bundle 分层组合(2d) | 扩展 | §11.4 P2-3 |
| **P2-12** | **Q9** Agent Team / 多 Agent 协作(2d) | 扩展 | §14.3(可选)|

### 4.4 总预算

| 阶段 | 工作量 | 价值 |
|---|---|---|
| P0 | 3.5d | 极高(立即解 L3 多工具 + 用户切换诉求)|
| P1 | 10d | 高(月内高可用 + 高可靠基本到位)|
| P2 | 11d | 中(看需求)|
| **总计** | **~25d** | |

### 4.5 2 天"快速胜利"最小动作(如果只能动这么多)

如果业务压力大,只能用 2 天,推荐 **D1 + D3**(§11.7):

1. **Day 1 上午(0.5d):E1 `quote_provider` seam** — 回应"yfinance/eastmoney 可切换"
2. **Day 1 下午 - Day 2(1.5d):PTC 模式骨架** — 解决"深度分析"多工具并发失败

完成后用户立刻看到:
- 数据源可切换
- LLM 可以并发调 quote+fundamentals+news
- 现有功能完全兼容

---

## 五、不要做的事(避免过度设计)

| # | 想法 | 不做的原因 |
|---|---|---|
| 1 | 搬 Cordis 插件树 | Python 生态差异,dataclass + register 已够 |
| 2 | 搬 Branded IDs / merge-extensible map | TS 特性,Python 不需要 |
| 3 | 搬完整 72 个 capability seam | 我们用 5-6 个够 |
| 4 | 搬 Profile + Bundle | 单 web app 用不到 |
| 5 | 搬完整 append-only 全量历史 | 持久化成本,迁移成本 |
| 6 | 搬 6 个 subagent backend | web 端只 1 个 in-process |
| 7 | 搬完整 ConversationNodeAssembler | 加 `match/update/publication`/`buildViewNode` 全套太重 |
| 8 | 搬完整 bracketed AssistantStreamFrame | SSE dict 够用 |
| 9 | 搬 Worker-thread PTC backend | web 端无沙箱 |
| 10 | 搬 ACP / SDK / 多 profile CLI | 单一 web app |
| 11 | 搬 ReplayEnvelope opaque state | 没跨 session resume |
| 12 | 搬 Lossless JSON 严格序列化 | JSON.dumps 够用 |
| 13 | 搬 `ignorable?: true` 严格拒绝重建 | 没跨 session resume |
| 14 | 搬 Compaction 压缩历史 | 我们直接 truncation 200 条 |
| 15 | 搬 SessionHeader schema_version | 没跨版本升级场景 |

---

## 六、详细参考链接

### 6.1 调研主文件

- [`2026-09-14-deepseek-harness-architecture-study.md`](./2026-09-14-deepseek-harness-architecture-study.md)— 1551 行,§1-14 详细分析

### 6.2 按维度回看

| 本文件章节 | study doc 对应章节 |
|---|---|
| §1 高可用 | §12.2(我们的多 session)+ §11.3(可学设计)|
| §2 高扩展 | §11.2(我们的强项)+ §11.3(可学设计)+ §13(系统 prompt)+ §14.1(conversation)|
| §3 高可靠 | §11.3(可学设计)+ §12.1(memory 现状)+ §13(system prompt as event)+ §14.2(streaming)|
| §4 路线图 | 综合 §5 + §10.3 + §11.4 + §12.6 + §13.9 + §14.4 |
| §5 不要做 | §11.5 + §12.7 + §13.10 + §14.4 |

### 6.3 dsh 源文档(已抓到本地)

| 主题 | URL |
|---|---|
| 总览 | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md> |
| 中文 | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.zh.md> |
| 72 seam | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/capability-seams.md> |
| Tool 管线 | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/tool-execution-pipeline.md> |
| 工具规范 | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/tools.md> |
| Session log | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.md> |
| Subagent | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/subagent.md> |
| System prompt | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/system-prompt.md> |
| LLM streaming | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/llm-streaming.md> |
| Conversation | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/conversation.md> |
| Core | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/core.md> |
| Scope | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/scope.md> |
| 添加 plugin | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cookbook/adding-a-package.md> |
| 添加工具 | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cookbook/adding-a-tool.md> |
| 添加 LLM | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cookbook/adding-an-llm-adapter.md> |
| Extension cookbook | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cookbook/extension-cookbook.md> |
| Event matrix | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/event-producer-consumer.md> |
| Agent lifecycle | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/agent-lifecycle.md> |
| Cordis primer | <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cordis-primer.md> |
