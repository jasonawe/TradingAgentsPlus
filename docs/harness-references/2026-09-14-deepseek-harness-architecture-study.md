# DeepSeek Harness 架构深读 — 对 TradingAgentsPlus harness 设计的启示

> 调研日期: 2026-09-14
> 调研对象: [`deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness) (master, dsh 2026-08 开源)
> 调研者: TradingAgentsPlus harness owner
> 目的: 为 TradingAgentsPlus 通用 agent harness 设计找参考,先学后做,不盲目照搬

## 摘要(一句话)

dsh = **Cordis 插件树** + **72 个 capability seam** + **append-only session log** + **5 阶段 tool 管线** + **PTC 模式**(LLM 写真程序调工具)+ **6 个 subagent provider**。对我们最有价值的 4 个机制:**Capability Seam 完善、PTC 模式、tool 管线 5 阶段、subagent 命名 provider 注册表**。

---

## 一、为什么读这个项目

我们之前在做 TradingAgentsPlus 的"理财通用 agent harness"——把 watchlist / 行情 / 财务 / 新闻 / alpha 因子 / 笔记 / 告警 / 定时任务全部暴露给一个 LLM。做到 P1 阶段时碰到几个反复出现的问题:

1. LLM 想**并发**调 quote+fundamentals+news,我们是 5 步串行 plan-then-execute,LLM 把"工具调用失败"当成"我没招了"(stuck)。
2. 工具 Provider(行情数据源)绑死 eastmoney,无法换 yfinance / akshare,也不支持用户切换。
3. 工具只有 execute 阶段,没有 HITL 安全闸门 / 后置 fallback / 审计观测。
4. sub-agent(data/news/alpha/synthesizer)是写死的内部函数,无法替换后端。

dsh 是 DeepSeek 2026-08 开源的官方 agent harness(TypeScript/Node),正好是为多客户端(Claude Desktop / Web / ACP / SDK)+ 多场景(code review / research / agent team)设计的工业级参考。读它不是为了搬,是为了看"工业级 agent harness 长什么样"。

---

## 二、dsh 的 5 大设计骨架

#### 2.1 一切皆插件 — Cordis plugin tree

dsh 的"内核"是 [`cordis`](https://github.com/shigma/cordis)(也是他们自家 `cordis-primer.md` 里讲的),一个 2000 行 TS 插件框架。每一个 capability 都是一个插件,挂到共享 `Context` 上提供 service / event / effect。**没有特权内核**——要扩展 dsh,就把插件挂到现有插件旁边;卸载插件会撤销它注册的所有副作用。

**对我们:** 不需要搬 cordis(我们 Python,框架差异太大),但**"一切皆插件"的心智模型**值得学——我们现在 `tools_bridge.py` 把工具直接绑死实现,就是"特权内核"反模式。

#### 2.2 Profile + Bundle 分层组合

dsh 启动时是一棵插件树,由 `profile`(home 里的具名组装)+ `bundle`(配置项的分发格式)+ `cordis.patch.yml` 三层叠加而成:

```
dsh-base (model adapters / tools / persistence / sandbox / approval / settings / telemetry)
  ↓ patch
dsh-web-app / dsh-headless / dsh-sdk-app / dsh-acp-app
  ↓ patch
profile (用户的 cordis.patch.yml)
  ↓ patch
--patch CLI overlay
```

启动顺序由 `package.json` 的 `dsh.profile` / `dsh.bundle` 字段声明。`dsh --profile web --dump-config` 可以打印整棵配置树。

**对我们:** 不搬(我们没 profile 概念),但**"patch 而非 fork"**的思路好——加新能力不替换现有包,只是在它上面挂一个监听器。

#### 2.3 Capability Seam 三段式 — 文档化的扩展点

72+ 个 capability seam,每个 seam 三段式:

| 角色 | 谁做 | 做什么 |
|---|---|---|
| Service Definition | `pkg_X` (e.g. `llm`, `tools`, `subprocess`) | 在 `Context` 上声明 `ctx.llm` / `ctx.tools` / `ctx.subprocess`,只暴露 interface |
| Service Provider | `pkg_X_<backend>` (e.g. `llm-deepseek`, `llm-pi-ai`, `bash-local`, `sandbox-local`) | 实现 seam,声明 `Config`(用 schemastery 校验) |
| Consumer | 任意依赖 `ctx.X` 的插件 | 通过 `inject: ['X']` 注入,只引用 seam 不引用实现 |

**示例**(`capability-seams.md` 抽取):

| Seam | Definition | Providers | Consumers |
|---|---|---|---|
| `ctx.llm` (LLM adapter registry) | `pkg_llm` | `pkg_llm_deepseek`, `pkg_llm_pi_ai` | `pkg_agent_loop`, `pkg_compaction_basic`, `pkg_session_log_deepseek` |
| `ctx.subprocess` (子进程) | `pkg_subprocess` | `pkg_subprocess_local`, `pkg_e2b`, `pkg_pwsh_local` | `pkg_tool_bash`, `pkg_lsp_stdio`, `pkg_subagent_acp` |
| `ctx.shell` (bash executor) | `pkg_shell` | `pkg_bash_local`, `pkg_bash_sandbox`, `pkg_terminal_bash` | `pkg_tool_bash` |
| `ctx.fs` (文件系统) | `pkg_fs` | `pkg_fs_local`, `pkg_fs_e2b`, `pkg_fs_sandbox` | `pkg_tool_fs`, `pkg_file_reference_local` |
| `ctx.sandbox` (沙箱) | `pkg_sandbox` | `pkg_sandbox_local`, `pkg_sandbox_policy`, `pkg_fs_sandbox` | `pkg_subprocess_local`, `pkg_bash_sandbox` |
| `ctx.subagents` (subagent provider) | `pkg_subagent` | `pkg_subagent_spawn_in_process`, `pkg_subagent_fork_in_process`, `pkg_subagent_acp`, `pkg_subagent_codex`, `pkg_subagent_claude_code`, `pkg_subagent_dsh_sdk` | `pkg_tool_subagent` |
| `ctx.approval` (HITL 审批) | `pkg_user_approval` | `pkg_permission_presets` | `pkg_tool_bash`, `pkg_tool_fs` |
| `ctx.sessionPersistence` (会话持久化) | `pkg_session_persistence` | `pkg_session_persistence_jsonl`, `pkg_session_query_sqlite` | `pkg_session`, `pkg_agent`, `pkg_session_query` |

完整 72 个 seam 见 [`capability-seams.md`](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/capability-seams.md)。

**对我们:** 这是**最强的可学之处**。我们现在 `tools_bridge.py` 把工具直接绑死 eastmoney,就是缺少 quote_provider seam。改造路径见 §五。

#### 2.4 Append-only Session Log + Surface Projection

dsh 的会话状态分两层:

1. **Append-only Session Log**(`ctx.sessionPersistence`):每次 turn 的事件都是 `SessionEvent`(session/*, agent/*, tool/*, request/*),只能追加不能改。`SCHEMA_VERSION` 单调递增,迁移用 `sessions.create(id, { seed })` 回放。
2. **SessionSurface projection**(`ctx.sessionProjections`):从 log 派生出来的只读视图,给 UI 用。可以替换 fold,不影响 log。

**关键事件**(从 `session.md` 抽取):

- 请求层: `request/header`, `request/context`
- 模型层: `agent/pre-step`, `agent/request`, `agent/request-error`, `agent/error`
- 流式: `agent/assistant-stream`, `agent/created`, `agent/disposed`
- 工具层: `tool/call`, `tool/result`, `tool/ptc-dispatch`, `tool/owned` (todo/write, fs/observed, hook/invoked, hook/result)
- Turn 边界: `turn/start`, `turn/end`, `step/start`, `step/end`, `agent/turn-stopping`
- 配置变更: `system-prompt/change`, `agent-preset/selected`

**对我们:** **不要搬**。我们的 5 节点状态机足够,append-only log + 派生 surface 是为"resume-from-history / 多客户端同步"设计的,我们目前没这个需求(只有一个 Web 客户端,无 resume)。

#### 2.5 Tool 执行管线 5 阶段 — 强烈推荐照搬

```
model → tool/call (session event, 落盘)
       ↓
   tools/pre-execute (waterfall, hooks/permission/sandbox) ← 允许/拒绝/问
       ↓ allow/ask
   registered monotonic guards (终态拒绝,后续监听器无法覆盖) ← 安全闸
       ↓ allow
   tools/execute (around-dispatch waterfall, timeout/retry/metrics) ← 包装
       ↓
   tool body → fs/write-intent / fs/read-intent (only tool-fs) ← 写盘保护
       ↓
   tools/post-execute (waterfall, accept/block/replace/attach-context) ← 校验/fallback
       ↓
   definition-owned finalizeContent (last content-only invariant) ← 渲染前最后兜底
       ↓
   tools/result (synchronous notification, frozen authoritative outcome) ← 审计/UI
       ↓
   tool/result (session event, single model-facing outcome)
       ↓
   presentResult(args, result) → UI card
```

完整 Mermaid 图见 [`tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/tool-execution-pipeline.md)。

**对我们:** 这 5 阶段是我们最该学的。我们现在 `tools_bridge.execute()` 直接调工具返回,缺 4 个关键环节:

| 阶段 | 我们当前 | 应该补 |
|---|---|---|
| pre-execute | 无 | 加危险工具(下单/删除告警)的权限闸门 |
| monotonic guards | 无 | 加终态拒绝,绕不过 hooks(防 LLM 自己改 prompt 绕过) |
| tools/execute | 简陋 | 包 around-dispatch,加 timeout / retry / metrics |
| post-execute | 无 | L3 judge 校验失败时换 fallback content(就是上次"unknown tool ''"的根因) |
| tools/result | 无 | 审计日志 / 指标 / UI 渲染入口 |

---

## 三、PTC 模式 — 杀手锏,直接对应我们之前的问题

dsh 的 **PTC = Program That Calls**(`cookbook/extension-cookbook.md` + `tool-execution-pipeline.md` 提及)。LLM 不返回 `{name, args}` JSON 列表,而是**写一段真程序**:

```ts
// LLM 生成的(简化示意)
async function plan(ctx) {
  const quote = await ctx.tools.get_quote({ symbol: '600036.SS' });
  const fundamentals = await ctx.tools.get_fundamentals({ symbol: '600036.SS' });
  const news = await ctx.tools.get_news({ symbol: '600036.SS', lookback_days: 30 });
  const factors = await ctx.tools.list_alpha_factors({ symbol: '600036.SS' });
  if (quote.price > fundamentals.pb_band.upper) {
    return ctx.tools.finalize({ verdict: 'overvalued', reasons: [...] });
  }
  return ctx.tools.finalize({ verdict: 'fair', reasons: [...] });
}
```

控制流是 free if/else/for/try/await,每个子调用走完整的 5 阶段管线,父 token 贯穿,所有 sub-call 都通过 `tools/ptc-dispatch-log` waterfall 落盘。子调用可以嵌套,`parent` token 标识嵌套关系,模型不能绕过校验。

**对我们:** **核心问题解决器**。之前 L3 失败的两例:

- 例 1: `深度分析 600036.SS 估值` → LLM 想并发调 quote+fundamentals+news,但 plan-then-execute 是串行,LLM 返回的 plan 没问题,但 execute 时工具链断了(`unknown tool ''`)
- 例 2: `页面错乱 + 失败信息输出` → 同一根因

PTC 模式下,LLM 直接写 `await get_quote + await get_fundamentals + await get_news`(并发 `Promise.all`),中间可以 `if/else` 决策,自然处理失败 / fallback。

---

## 四、Subagent Provider 注册表 — 我们马上能用

dsh 的 `ctx.subagents` 注册 6 个 provider,每个声明 capabilities:

| Provider | 启动位置 | depthLimit | agentOptions | toolFilter | persona |
|---|---|---|---|---|---|
| `subagent-spawn-in-process` | 同一进程新开 Agent | ✓ | ✓ | ✓ | ✓ |
| `subagent-fork-in-process` | 同一进程 fork 现有 Agent | ✓ | ✓ | ✓ | ✓ |
| `subagent-acp` | 通过 ACP 协议远程 | ✗ | ✗ | ✗ | ✗ |
| `subagent-codex` | Codex CLI | ✗ | ✗ | ✗ | ✗ |
| `subagent-claude-code` | Claude Code CLI | ✗ | ✗ | ✗ | ✗ |
| `subagent-dsh-sdk` | DSH SDK JSON-RPC | ✓ | ✓ | ✗ | ✗ |

调用方按 `provider='alpha', task=...` 选后端。

**对我们:** 我们现在的 `data_agent` / `news_agent` / `alpha_agent` / `synthesizer` 都是写死内部函数。改成注册表后:

- 加 `qstock`、`akshare-fundamental` 只需注册,不需改 orchestrator
- 未来想接外部 agent(通义/Claude)只需加 provider
- LLM 可以用 `delegate(provider='alpha', task=...)` 自由选择

---

## 五、对 TradingAgentsPlus 的精确启示 + 实施路线

### 5.1 我们"现在就能改"的 4 个高价值点

| # | 动作 | 工作量 | 价值 | 直接回应用户上轮诉求 |
|---|---|---|---|---|
| **P0-1** | 抽出 `quote_provider` seam:`eastmoney \| yfinance \| akshare \| stub` 注册表,`tools_bridge` 只引用 seam | 0.5 天 | 高 | ✅ 用户上轮问 "yfinance 或 eastmoney 可以让用户自己选吗" |
| **P0-2** | 引入 PTC 模式:LLM 二选一(返回 TS-like 字符串 或 JSON tool_calls),前者并发调度后者保持 | 1.5 天 | **极高** | ✅ 解决 L3 多工具并发/L3 judge 失败 |
| **P1-1** | 加 monotonic guards + tools/result 观测,补齐 5 阶段管线 | 0.5 天 | 中 | 安全/可观测 |
| **P1-2** | data/news/alpha 子代理改 named provider,挂 subagent 注册表 | 1 天 | 中 | 扩展性 |

### 5.2 不要搬的(避免过度设计)

| dsh 特性 | 为什么不要 |
|---|---|
| **Cordis 插件树** | 我们 Python,生态差异;我们用 dataclass + register 就够 |
| **Profile + Bundle** | 我们只有一个 web app,没多 profile 需求 |
| **Append-only Session Log** | 我们没跨客户端 / resume 需求,5 节点状态机足够 |
| **13 种 SessionEvent + merge-extensible** | dataclass enum 就够 |
| **Branded IDs + ts cordis-catalog 宏** | 强类型元编程,Python 收益小 |
| **PTC worker-thread backend** | 我们只做 web,无沙箱 |
| **ACP / SDK / 多 profile CLI** | 我们是单一 web app |

### 5.3 推荐执行顺序

```
Day 1 (0.5d): P0-1 quote_provider seam
Day 2-3 (1.5d): P0-2 PTC 模式(分两阶段:① TS-like 程序沙箱 ② LLM prompt 引导)
Day 4 (0.5d): P1-1 monotonic guards + tools/result 观测
Day 5 (1d): P1-2 subagent provider 注册表
```

### 5.4 每个动作的验收标准

**P0-1 quote_provider seam**

- `tradingagents/agent_harness/providers/quote.py` 定义 interface
- 4 个实现:`eastmoney.py` / `yfinance.py` / `akshare.py` / `stub.py`
- `tools_bridge.get_quote()` 通过 registry 选 provider,不直接绑 eastmoney
- 设置页加 data source selector,重启生效
- 测试:4 个 provider 都能调通;切 stub 后所有调用走 stub

**P0-2 PTC 模式**

- `tradingagents/agent_harness/ptc.py` 定义 `PTCProgram` schema
- `orchestrator.py` 检测 LLM 返回 `mode: 'ptc'` 时切到 PTC 调度
- PTC 调度器用 `asyncio.gather` 并发子调用,每个子调用走完整管线
- `tools/ptc-dispatch-log` 落盘所有子调用
- 测试:① LLM 返回 PTC 程序时并发执行 ② 子调用失败时主程序可 try/except ③ 单个 PTC 总 timeout 30s

**P1-1 tool 管线 5 阶段**

- `tradingagents/agent_harness/tools/pipeline.py` 实现 5 阶段
- `pre_execute` waterfall:危险工具名单(delist_alert / cancel_schedule)返回 `ask`
- `monotonic_guards`:不可逆工具(del 所有 watchlist)的最终拒绝闸
- `tools/execute` around-dispatch:timeout + retry(2 次指数退避) + metrics
- `tools/post_execute`:L3 judge 失败时换 fallback content
- `tools/result`:审计日志(`logs/tool_audit.jsonl`)+ 指标(prometheus)

**P1-2 subagent provider 注册表**

- `tradingagents/agent_harness/subagents/registry.py` 定义 `SubagentProvider`
- 4 个 provider:`DataAgent`(本地查 quote/fundamentals)/ `NewsAgent`(本地 stub 接 news API)/ `AlphaAgent`(本地 alpha158 计算)/ `SynthesizerAgent`(纯 LLM 总结)
- `tool_subagent(provider, task)` 暴露给 LLM
- 测试:4 个 provider 都能 invoke;LLM 能选 provider

---

## 六、源文档索引(本次调研全部读全文或关键段)

### 6.1 主架构文档

| 文档 | URL | 用途 | 读法 |
|---|---|---|---|
| [architecture.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/architecture.md) | 总览(160 行) | 全文读 |
| [architecture.zh.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/architecture.zh.md) | 中文版(164 行) | 全文读 |
| [capability-seams.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/capability-seams.md) | 72 个 seam 总图(49KB) | Mermaid 图 + 关键表 |

### 6.2 Cordis 基础

| 文档 | URL | 用途 |
|---|---|---|
| [cordis-primer.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/cordis-primer.md) | Cordis 入门(45 行) | 全文读 |
| [cordis-tutorial/index.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/cordis-tutorial/index.md) | 7 章 tutorial 入口(60 行) | 全文读 |

### 6.3 子系统

| 文档 | URL | 用途 | 读法 |
|---|---|---|---|
| [subsystems/core.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/core.md) | Agent handle + API(66KB) | 关键 type-equiv |
| [subsystems/tools.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/tools.md) | Tool 生命周期 + DSL + 管线(42KB) | 全文 170-410(执行管线) |
| [subsystems/session.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/session.md) | SessionEvent + Surface(67KB) | 事件词汇表(166-275) |
| [subsystems/subagent.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/subagent.md) | SubagentProvider + 6 个后端(56KB) | 全文 38-130(one-shot + provider contract) |
| [subsystems/system-prompt.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/system-prompt.md) | System prompt 装配 + section / variable(11KB) | 全文读 |
| [subsystems/llm-streaming.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/llm-streaming.md) | LLM 流式协议(62KB) | 跳读(协议规范) |
| [subsystems/conversation.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/conversation.md) | Conversation 节点(17KB) | 全文读 |
| [subsystems/scope.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/scope.md) | Scope 模型 | 跳读 |
| [subsystems/agent-team.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/agent-team.md) | Agent team | 跳读 |
| [subsystems/webhook.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/subsystems/webhook.md) | Webhook | 跳读 |

### 6.4 Cookbook(最实用)

| 文档 | URL | 用途 |
|---|---|---|
| [cookbook/extension-cookbook.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/cookbook/extension-cookbook.md) | 新增功能机制表(11KB) | **全文读**(产品特性→插件机制表最实用) |
| [cookbook/adding-a-tool.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/cookbook/adding-a-tool.md) | defineTool 完整规范(14KB) | **全文读** |
| [cookbook/adding-an-llm-adapter.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/cookbook/adding-an-llm-adapter.md) | LlmAdapter 子类化(4KB) | **全文读** |
| [cookbook/adding-a-package.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/cookbook/adding-a-package.md) | 新建 package checklist(12KB) | **全文读** |
| [cookbook/adding-a-settings-card.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/cookbook/adding-a-settings-card.md) | 设置卡片(6KB) | **全文读** |

### 6.5 流程 / 事件

| 文档 | URL | 用途 |
|---|---|---|
| [tool-execution-pipeline.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/tool-execution-pipeline.md) | Tool 管线 Mermaid 图(4KB) | **全文读** |
| [agent-lifecycle.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/agent-lifecycle.md) | Agent 生命周期 sequence(5KB) | 全文读 |
| [event-producer-consumer.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/event-producer-consumer.md) | 事件生产者-消费者矩阵(24KB) | 关键表格 |

### 6.6 配置参考(参考性,不深读)

| 文档 | URL |
|---|---|
| [config-catalog.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/config-catalog.md) | 配置项目录(155KB,自动生成) |
| [cordis-api/context.md](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/vendor/cordis-api/context.md) | Cordis Context API |

### 6.7 已废弃/不存在(确认过)

| 路径 | 原因 |
|---|---|
| `docs/subsystems/agent-loop.md` | 不存在,agent loop 文档合并到 `architecture.md` + `subsystems/core.md` |
| `docs/packages/core/agent-loop/README.md` | 路径错了,代码在 `packages/core/agent-loop/`,但没 README |
| `docs/packages/bundle/base/README.md` | 路径错了 |

---

## 七、关键引用(代码片段,直接对应我们改的代码)

### 7.1 Capability Seam 三段式(`capability-seams.md` 提炼)

```ts
// pkg_llm/index.ts (Service Definition)
export const name = 'llm'
export const inject = ['logger']
export function apply(ctx: Context) {
  ctx.llm = { /* interface */ registerAdapter, resolveModel, getAdapter }
}

// pkg_llm_deepseek/index.ts (Service Provider)
export const name = 'llm-deepseek'
export const inject = ['llm']
export const Config: z<Config> = z.object({ apiKey: z.string() })
export function apply(ctx: Context, config: Config) {
  ctx.llm.registerAdapter(['deepseek'], new DeepSeekAdapter(config))
}

// pkg_agent_loop/index.ts (Consumer)
export const name = 'agent-loop'
export const inject = ['llm']
export function apply(ctx: Context) {
  ctx.effect(() => {
    const adapter = ctx.llm.getAdapter('deepseek', 'deepseek-chat')
    // ...
  })
}
```

**对应 Python 改法**(我们 `tradingagents/agent_harness/providers/quote.py`):

```python
# providers/quote.py (Service Definition)
class QuoteProvider(Protocol):
    async def get_quote(self, symbol: str, **kwargs) -> QuoteResult: ...

class QuoteRegistry:
    def __init__(self):
        self._providers: dict[str, QuoteProvider] = {}
    def register(self, name: str, provider: QuoteProvider): ...
    def get(self, name: str) -> QuoteProvider: ...

# providers/eastmoney.py (Service Provider)
class EastMoneyQuoteProvider(QuoteProvider):
    async def get_quote(self, symbol: str, **kwargs) -> QuoteResult: ...

# tools_bridge.py (Consumer, 之前是直接绑 eastmoney)
class ToolsBridge:
    def __init__(self, registry: QuoteRegistry):
        self._quotes = registry
    async def get_quote(self, symbol, provider='eastmoney', **kwargs):
        return await self._quotes.get(provider).get_quote(symbol, **kwargs)
```

### 7.2 Tool 5 阶段管线(`tool-execution-pipeline.md` + `tools.md`)

```ts
// tools.ts
async function runToolPipeline(input: ToolExecutionInput): Promise<ToolExecutionResult> {
  // 1. tools/pre-execute waterfall
  const preDecision = await ctx.emitter.waterfall('tools/pre-execute', input)
  if (preDecision === 'deny') return deniedResult()
  if (preDecision === 'ask') return await askUser(input)

  // 2. monotonic guards (终态拒绝)
  for (const guard of registeredGuards) {
    if (!guard(input)) return deniedResult('guard')
  }

  // 3. tools/execute (around-dispatch)
  return await ctx.emitter.around('tools/execute', async () => {
    // 4. post-execute
    const result = await ctx.tools.executeInternal(input)
    const postResult = await ctx.emitter.waterfall('tools/post-execute', result)
    // 5. tools/result (synchronous notification)
    ctx.emitter.emit('tools/result', postResult)
    return postResult
  })
}
```

**对应 Python 改法**(我们 `tradingagents/agent_harness/tools/pipeline.py`):

```python
async def run_tool_pipeline(input: ToolExecutionInput) -> ToolExecutionResult:
    # 1. pre-execute
    pre = await waterfall('tools/pre-execute', input)
    if pre.deny: return denied(pre.reason)
    if pre.ask: return await ask_user(input)

    # 2. monotonic guards
    for guard in monotonic_guards:
        if not guard(input): return denied('guard')

    # 3-4. execute + post-execute
    async def _do():
        result = await ctx.execute_internal(input)
        return await waterfall('tools/post-execute', result)

    # 5. tools/result observation
    final = await around('tools/execute', _do, timeout=30, retry=2)
    emit('tools/result', final)
    return final
```

### 7.3 SubagentProvider(`subsystems/subagent.md` 关键)

```ts
// subagent.ts
interface SubagentProvider {
  readonly name: string
  readonly capabilities: SubagentCapabilities  // depthLimit / agentOptions / toolFilter / persona
  start(request: ResolvedSubagentStartRequest): Promise<SubagentRun>
}

// subagent-spawn-in-process.ts
export const name = 'subagent-spawn-in-process'
export const inject = ['subagents']
export const Config: z<Config> = z.object({ maxDepth: z.number().default(3) })
export function apply(ctx: Context, config: Config) {
  ctx.subagents.registerProvider({
    name: 'spawn-in-process',
    capabilities: { depthLimit: true, agentOptions: true, toolFilter: true, persona: true },
    async start(req) { /* create new Agent in same process */ }
  })
}
```

**对应 Python 改法**(我们 `tradingagents/agent_harness/subagents/registry.py`):

```python
class SubagentProvider(Protocol):
    name: str
    capabilities: SubagentCapabilities
    async def start(self, req: SubagentStartRequest) -> SubagentRun: ...

class SubagentRegistry:
    def __init__(self):
        self._providers: dict[str, SubagentProvider] = {}
    def register(self, provider: SubagentProvider): ...
    async def start(self, provider_name: str, req) -> SubagentRun: ...
```

---

## 八、反思 — dsh 不是万能药

dsh 是为"多客户端(Claude Desktop / Web / ACP / SDK)+ 多 profile(无头 / 交互 / 自动批)"设计的工业级 harness,**复杂度高,学习成本大**。我们没这个需求,所以:

- **不搬 cordis**(我们 Python,生态不同;dataclass + register 已经够)
- **不搬 append-only log**(没多客户端)
- **不搬 branded IDs + 类型宏**(Python 收益小)
- **不搬 worker-thread PTC backend**(我们只 web,无沙箱)

但**心智模型**值得学:**"一切皆插件 + capability seam + tool 管线 5 阶段 + subagent 注册表"**。这 4 个心智模型可以让我们的 harness 从"能跑"变成"可演化"。

---

## 九、参考资源

- dsh GitHub: <https://github.com/deepseek-ai/deepseek-harness>
- DeepWiki 解读: <https://deepwiki.com/deepseek-ai/deepseek-harness>
- 我们当前的 harness: `tradingagents/agent_harness/`(orchestrator / tools_bridge / providers / builtin)
- 我们之前的实现 notes: `.agents/notes/implemented/`

---

## 十、Session 与 Context — 深度补读

> 在原 doc 起草时,我对 session 和 context 只给了概览(§2.4 + §2.1)。本节是补读,从 `subsystems/session.md`(67KB)+ `core.md`(66KB)+ `subsystems/scope.md` 提炼真正可用的细节。

### 10.1 SessionEvent 的本质

dsh 的 **Session = append-only event log**,不是"消息列表"。每次状态变化都 append 一条 `SessionEvent`,只能追加不能改 / 不能删。模型看到的消息列表是从 log **派生**出来的。

#### 10.1.1 事件分类 — 4 surface + N log-only

`SurfaceEventType` 是 **唯一** 4 个会产生 LLM 上下文的事件:

```ts
type SurfaceEventType =
  | 'system/message'   // 渲染后的 system prompt
  | 'user/message'     // 用户 / inject / goal continuation
  | 'assistant/message' // 模型输出(含 stream)
  | 'tool/result'      // 工具结果(单条 model-facing outcome)
```

其他都是 **log-only**(有 `seq` 有 `time` 有 `data`,但不进 LLM context):

- `turn/start` / `turn/end` — turn 边界
- `step/start` / `step/end` — step 边界
- `assistant/attempt` — 模型失败重试,无 surface message
- `request/header` / `request/context` — 路由元数据
- `agent/created` / `agent/disposed` / `agent/error` — agent 生命周期
- `compaction/*` — 压缩(pluggable)
- `hook/invoked` / `hook/result` — hook 桥接
- `session/end-seed` — fork 边界 marker
- 其他 plugin-contributed log-only 事件(merge-extensible)

#### 10.1.2 SessionEvent 结构 — 判别联合,不是字段拼凑

```ts
type SessionEvent<T extends SessionEventType> = {
  type: T                        // 判别字段,switch 时自动窄化 data
  seq: SessionSeq                // monotonic,branded
  time: number                   // epoch ms
  data: SessionEventMap[T]       // 类型由 type 决定
  ignorable?: true               // 缺失=必读,识别不出就拒绝重建
} & (T extends SurfaceEventType ? SurfaceIntent<T> : {
  surfaceOp?: never              // log-only 事件编译期禁止
  sourceEventSeqs?: never
})
```

关键设计:

- **判别联合,不是独立 type/data 联合** — `switch (event.type)` 自动窄化 `event.data`,无需 cast
- **`ignorable?: true`** — 缺失视为必读,reader 遇到不识别的必读事件直接拒绝重建(避免静默丢事件导致状态错乱)
- **SurfaceIntent 是条件类型** — 编译期就保证 log-only 事件不会带 `surfaceOp`,编译器帮你抓错

#### 10.1.3 SurfaceOp — 怎么把事件挂到 surface

```ts
type SurfaceOp =
  | 'append'                                          // 正常追加
  | { op: 'replace'; startSeq: SessionSeq; endSeq: SessionSeq }  // 替换一段
```

`replace(startSeq, endSeq)` shadows [startSeq, endSeq] 区间的所有 surface 节点,新节点插入原位。要点:

- `startSeq === endSeq` 替换单节点
- 端点必须在当前 surface 里存在
- 端点的 seq 是 surface 顺序,不是数字顺序
- `sourceEventSeqs` 必须包含被替换的所有 surface node 的 seq(审计 / 回放需要)

典型用例:**compaction** 把 50 条历史消息替换成 1 条 summary,生成 `replace(startSeq, endSeq)` 事件,`sourceEventSeqs` 列出被替换的 50 条。

#### 10.1.4 TurnEndReasonMap — turn 为什么结束

```ts
type TurnEndReason =
  | { kind: 'completed' }   // 正常完成
  | { kind: 'cancelled' }   // 用户取消
  | { kind: 'errored'; error: ... }  // 错误
  | { kind: 'max-tokens' }  // 模型截断(整个 turn 都标 max-tokens,即使后续有续写)
  | { kind: 'interrupted' } // 仅由 crash recovery 合成
```

merge-extensible,plugin 可以加自己的 reason。

#### 10.1.5 session/end-seed — fork 边界 marker

dsh 把 fork seed 和 live work 用一个 marker 隔开:`session/end-seed { inherited: true }` 在继承的 seed 末尾,`session/end-seed {}` 在新 fork 末尾。这样:

- crash recovery 能区分"crash 时正在跑的 compaction"vs"刚从历史里加载的 compaction"
- 读取历史时能从最后 `inherited: true` marker 切回 live work
- ordering by human activity 排除这个 marker(打开 session 不是工作)

#### 10.1.6 Agent 生命周期 — 6 个能力

```ts
interface Agent {
  followup(message: UserMessage): void                // 普通后续 turn,唤醒
  steer(message: UserMessage): void                   // 下一步插入,运行中也可
  inject(message: UserMessage): void                  // 注入上下文但不唤醒
  send(message: UserMessage, target: InboxTarget, wakeup: boolean): void  // 投递到 inbox 边界
  runMaintenance<T>(task): Promise<T>                 // 同步维护任务
  whenIdle(): Promise<void>                           // 等到 idle
  cancel(): void                                      // 取消
  dispose(): Promise<void>                            // 卸载
}
```

加 `AgentStatus = 'idle' | 'running'`,`running` 覆盖整个 drain 区间(包括连续 queued turns),不证明 turn 还开着。

每个事件流都是 bracketed:

```ts
type AssistantStreamFrame =
  | { type: 'start'; attemptId, revision, turn, step }
  | { type: 'chunk'; attemptId, revision, index, time, chunk }
  | { type: 'end'; attemptId, revision, index, outcome: 'committed' | 'abandoned' }
```

`revision` 是 monotone,replacement 重启时从 1 开始。

### 10.2 Context — Cordis 根对象 + Scope

dsh 的 `Context` 是 Cordis 的根,所有 plugin 挂到共享 Context。`ctx.<service>` 是命名服务(72+ 个 seam),`ctx.inject(['X'])` 声明依赖。**Scope** 是核心抽象,per-agent 隔离所有注册。

#### 10.2.1 Scope 三件套

```ts
import { createScope, scopeOf, scopeTarget } from '@deepseek-ai/dsh-scope'

const subScope = createScope(parent)         // 创建子 scope
scopeOf(registration) === subScope           // 查询注册所属 scope
scopeTarget(registration) === parent         // 查询注册的目标
```

Scope 让"per-agent 注册"成为 first-class 操作:

- agent 自带的 system prompt section、tool registration、prompt variable 都挂在 agent scope 上
- agent 卸载时 scope 整体撤销
- subagent 的 toolFilter 通过 `scope.restrict()` 实现(命名工具消失 + 拒绝执行,**一条 visibility**)

#### 10.2.2 Plugin 三段式

```ts
// pkg_X/index.ts
export const name = 'X'
export const inject = ['A', 'B']              // 依赖的服务
export const Config: z<Config> = z.object({...})  // 配置(用 schemastery 校验)

export function apply(ctx: Context, config: Config) {
  ctx.X = { /* service 接口 */ }

  ctx.effect(() => {
    // 注册 service / event / listener,卸载时自动撤销
    ctx.on('foo/bar', (event) => {...})
    ctx.emit('foo/registered')
    return () => { /* disposer(可选) */ }
  })
}
```

每个 plugin 都有:
- `name` — 唯一标识
- `inject` — 依赖服务列表
- `apply(ctx, config)` — 安装函数(可返回 disposer)

#### 10.2.3 Event 三种模式

```ts
// emit — 广播,无返回值
ctx.emit('foo/bar', payload)

// waterfall — 链式,返回第一个非 undefined 值
const decision = await ctx.waterfall('agent/pre-step', input)
// listener 1 -> undefined, listener 2 -> 'deny' → 整个 waterfall 返回 'deny'

// around — 包裹,around 模式可换上下文
const result = await ctx.around('tools/execute', async () => {
  return await toolBody(input)
}, timeoutMs)
// 可在 around 里替换 signal,加 timeout,捕获异常
```

这三种模式对应了 `tools/pre-execute` (waterfall 决策) / `tools/execute` (around 包裹) / `tools/post-execute` (waterfall 校验) / `tools/result` (emit 通知) 的完整语义。

### 10.3 对 TradingAgentsPlus 的精确启示(更新版)

#### 10.3.1 立即可学 — 4 个高价值点(更新原 doc §5.1)

| # | 动作 | 工作量 | 价值 | 直接对应 |
|---|---|---|---|---|
| **P0-A** | **Surface event 分类** — 把我们的"消息事件"拆成 surface(4 类进 LLM 上下文)vs log-only(N 类只落审计)。前端 SSE 推送只推送 surface 事件。 | 0.5 天 | 高 | **统一事件流,前端简化** |
| **P0-B** | **SurfaceOp.replace 模型** — 我们的 5 节点状态机"重新评估"步骤,显式记录 `replace(startSeq, endSeq)`,而非"我重写了历史"。回放 / 审计更清楚。 | 0.5 天 | 高 | **审计可回放** |
| **P1-A** | **Agent 6 能力** — 加 `steer`(中途改主意)、`inject`(定时任务结果注入,不唤醒)、`whenIdle()`(等 drain) | 1 天 | 中 | **定时任务 + 用户干预** |
| **P1-B** | **Per-agent scope** — sub-agent 创建独立 scope,tool registry / LLM adapter / prompt 都在自己 scope,卸载时整体撤销 | 1 天 | 中 | **sub-agent 隔离** |

#### 10.3.2 仍然不要搬

| dsh 特性 | 为什么不要 |
|---|---|
| **Branded SessionSeq / BrandedNumber** | 我们不需要类型层面的强保证,普通 int 就够 |
| **merge-extensible event map**(declaration merging) | TS 特性,Python 没等价物 |
| **`ignorable?: true` 严格拒绝重建** | 我们没跨 session resume 需求,fallback 策略直接重试就行 |
| **`session/end-seed` marker** | 我们没 fork + crash recovery 的复合场景 |
| **`AssistantStreamFrame` 完整 bracketed** | 我们 SSE 推送用 dict 就够 |
| **64KB session.md 全量阅读** | 除非真要做多客户端 / fork / resume,不必读那么深 |

#### 10.3.3 推荐的实施优先级(综合 §5.1 + §10.3.1)

```
Day 1 (0.5d): P0-1 quote_provider seam        (回用户上轮诉求)
Day 1 (0.5d): P0-A Surface event 分类          (统一事件流)
Day 2 (0.5d): P0-B SurfaceOp.replace 模型     (审计可回放)
Day 3-4 (1.5d): P0-2 PTC 模式                 (核心痛点)
Day 5 (0.5d): P1-1 monotonic guards + tools/result 观测
Day 6 (0.5d): P1-A Agent 6 能力 (steer/inject/whenIdle)
Day 7 (1d): P1-2 subagent provider 注册表
Day 8 (1d): P1-B Per-agent scope
```

### 10.4 关键引用 — SessionEvent 字段在我们 harness 的对应

| dsh 字段 | 我们当前 | 应该改 |
|---|---|---|
| `type` | 没强类型,`event["type"]` 字符串 | 用 `Literal["turn/start", "tool/call", ...]` Enum |
| `seq` | 用 timestamp | 加 monotonic counter(branded 不必) |
| `data` | dict | `TypedDict` per type |
| `surfaceOp` | 无 | 显式 `append` / `replace` |
| `sourceEventSeqs` | 无 | 审计要追溯"我是从哪些历史节点派生来的" |
| `ignorable` | 无 | fallback 策略参考:识别不出必读事件 → 拒绝重建 |

### 10.5 关键引用 — Agent 6 能力的 Python 对应

```python
# tradingagents/agent_harness/agent.py
class Agent(Protocol):
    async def followup(self, message: UserMessage) -> MessageId: ...   # 已有
    async def steer(self, message: UserMessage) -> MessageId: ...      # 新增(中途改主意)
    async def inject(self, message: UserMessage) -> MessageId: ...     # 新增(定时任务结果)
    async def send(self, message: UserMessage, target: InboxTarget, wakeup: bool) -> MessageId: ...
    async def run_maintenance(self, task: Callable) -> Any: ...        # 新增(同步维护)
    async def when_idle(self) -> None: ...                             # 新增(等 drain)
    async def cancel(self) -> None: ...                                # 已有
    async def dispose(self) -> None: ...                               # 已有
```

### 10.6 关键引用 — Scope 的 Python 对应

```python
# tradingagents/agent_harness/scope.py
class Scope:
    """Per-agent scoped registration container.

    Every tool / prompt / variable registration is bound to a scope.
    Disposing the scope disposes every registration — sub-agent 隔离核心。
    """
    def __init__(self, parent: Scope | None = None): ...
    def restrict_tools(self, allowed: list[str]) -> None: ...  # 对应 dsh scope.restrict()
    def effect(self, fn: Callable[[], Callable | None]) -> Callable[[], None]: ...  # 注册 + 自动撤销

# 用法:sub-agent 创建
sub_scope = Scope(parent=main_scope)
sub_scope.restrict_tools(['get_quote', 'get_news'])  # 只给数据工具,不给删除类
sub_agent = create_agent(scope=sub_scope, ...)
# 卸载时:sub_scope.dispose() 自动撤销所有注册
```

### 10.7 反思 — 为什么 dsh 这么重

dsh 设计这么重(cordis 2000 行 + 72 seam + 5 阶段管线 + append-only log)是为了:

1. **多客户端**(Claude Desktop / Web / ACP / SDK)— 每个客户端需要从同一份 log 派生不同视图(surface)
2. **跨 session fork + resume** — fork seed + inherited marker 让 fork 和 resume 语义清晰
3. **plugin 互不耦合** — 任何 capability 都能被替换 / 卸载,不留"特权内核"
4. **强类型安全** — branded ID + 判别联合 + conditional type 让 TS compiler 帮你抓错

我们没这 4 个需求,所以大部分机制是过度设计。但**心智模型**(surface vs log-only / scope 隔离 / event 三种模式 / 5 阶段管线)可以低成本迁移。
