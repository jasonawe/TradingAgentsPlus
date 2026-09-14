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
