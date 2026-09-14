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

---

## 十一、TradingAgentsPlus vs DeepSeek Harness — 完整对比

> 之前 §5 / §10.3 是"我们要学什么"。本节是**完整 side-by-side** — 我们的每个组件对应 dsh 的什么、差在哪、谁做得更好。

### 11.1 组件映射表(我们的 → dsh 的)

| 我们的组件 | 文件 / 行数 | dsh 对应 | 差异 |
|---|---|---|---|
| `Harness`(根容器) | `harness.py`(未读) | `Context`(cordis 根对象)+ 72 个 seam | 我们是单一 root;dsh 是命名 service 总线 |
| `Orchestrator`(5-node FSM) | `core/orchestrator.py` 545 行 | `AgentLoop` + 6 个状态机 | 都是显式状态机,差异不大 |
| `OrchestratorState`(mutable dataclass) | 同上 | `SessionEvent` log(append-only)+ `SessionSurface` projection | **关键差异** — 我们 mutable,dsh append-only |
| `ToolRegistry`(装饰器 + 手动 + entry_points) | `tools/registry.py` 119 行 | `ctx.tools` runtime | 我们是 dict,dsh 是 service + 5 阶段管线 |
| `BaseTool` / `FunctionTool` + Pydantic schema | `tools/base.py` 62 行 | `defineTool` + `ParameterSchemaSpec` DSL | dsh 多 `output.render` / `presentationMeta` |
| `PermissionType`(READ/WRITE/ADMIN enum) | `tools/permission.py` | `tools/pre-execute` waterfall + monotonic guards | dsh 是插件化的,我们是硬编码 enum |
| `Plugin` + `PluginRegistry` | `plugins/` 123 行 | Cordis plugin + `registerAdapter` | 都 OK,dsh 用 ctx.effect 自动 dispose,我们用 manual install |
| `entry_points` 发现(group=`agent_harness.plugins`) | `plugins/registry.py` | cordis.yml(必须 cd 进 repo) | **我们对** — Python 生态标准做法 |
| `LLMProvider` ABC + `LLM_REGISTRY` factory | `llm/` 4 文件 | `LlmAdapter` + `ctx.llm` registry | dsh 用 `usage before finish` 强约束,我们没 |
| `MemoryManager`(L1 session / L2 prefs / L3 refs) | `memory/` 5 文件 300+ 行 | session log + sessionFileReferences 混合 | **我们对** — 三层清晰 + SQLite + TTL |
| `Verifier`(L1/L2/L3,judge_factory 独立) | `core/verification.py` | `ctx.sessionLog` + `tool/result` observers | 我们的 L3 judge 解耦更直接 |
| `RetryPolicy` + `CircuitBreaker` | `core/retry.py` | `tools/execute` around-dispatch | 我们的独立,dsh 在 around 里 |
| `ShortCircuit`(Tier 1 快速路径) | `core/short_circuit.py` | 无 — 所有 turn 都走完整 loop | **我们对** — 简单查询跳过 5 节点 |
| `ToolContext`(session_id 单字段) | `tools/context.py` 21 行 | `ToolRunContext` + `scope` per-agent | dsh 多 `deferContext()` / `concludeTurn()` |
| `stream_chat()`(SSE AsyncIterator) | `orchestrator.py` | `AgentLoop` + `AssistantStreamFrame` bracketed | dsh 完整 bracketed 但复杂;我们简单 |
| HITL `_await_confirmation` | `orchestrator.py` 内 | `ctx.approval` + `permission_presets` | 都在,dsh 是独立 seam |
| `audit.log()`(单点) | `orchestrator.py` | `tools/result` 同步 emit + 30+ 事件监听器 | **dsh 强** — 全生命周期观测点 |

### 11.2 我们做得比 dsh 好的 10 个地方

| # | 我们的强项 | dsh 的对应 | 为什么我们更好 |
|---|---|---|---|
| **1** | **3-tier Memory 分层清晰** — L1 session / L2 preferences / L3 references,带 TTL,SQLite 持久化 | dsh 把这些混在 session log 里,靠 `sessionFileReferences` / `sessionSkillCatalog` Remote 兜底 | 我们更直接,Python 习惯 |
| **2** | **entry_points plugin 发现** — Python 生态标准做法 | dsh 必须 cd into repo 用 cordis.yml | 我们的可独立 pip install |
| **3** | **3 PermissionType 分级** — 简单 enum | dsh 用 `tools/pre-execute` waterfall 决策 | 我们更易读懂,够用 |
| **4** | **Pydantic 双 schema** — `args_schema` + `result_schema`,LLM args 自动校验 | dsh 用 ParameterSchemaSpec DSL(更复杂,带 JSON Schema 投射) | 我们更 Pythonic,自动序列化 |
| **5** | **L3 LLM-judge 完全解耦** — `judge_factory` 独立于主 LLM,可换模型 | dsh 把 judge 绑在主 loop,靠 `resolveModel()` 配 | 我们的更灵活 |
| **6** | **RetryPolicy + CircuitBreaker 单独抽出** — 可配置 | dsh 写在 around-dispatch 里,要注册 plugin | 我们的可见性更好 |
| **7** | **Streaming SSE 简单** — `AsyncIterator[(event, payload)]` | dsh 的 bracketed `AssistantStreamFrame` 完整但复杂 | 我们够用,简单胜出 |
| **8** | **Tier 1 短路径** — `fast_route()` 命中简单查询直接返回 | dsh 所有 turn 都走完整 5 节点 | **我们对** — 性能 + UX |
| **9** | **Plugin idempotency** — `install()` 检查重复 | dsh `register()` 抛错 | dsh 更"快速失败",我们更"宽容" |
| **10** | **Prompt 分发到 plugin** — 每个 plugin 提供自己的 prompt section | dsh 用 `systemPrompt.section()` 中心注册 | 一样;我们更分散 |

### 11.3 dsh 做得比我们好的 10 个地方(按价值排序)

| # | dsh 设计 | 我们的差距 | 价值 |
|---|---|---|---|
| **A** | **Tool 5 阶段管线**(pre / guards / execute / post / result) | 我们只有 `tool.invoke` + `circuit_breaker.allow()` + `audit.log`,没有正式阶段 | **极高** — 直接对应 L3 judge fallback / HITL 安全闸 |
| **B** | **PTC 模式**(LLM 写 async program) | 我们 LLM 只能返回 JSON tool_calls | **极高** — 直接解决 L3 多工具并发失败 |
| **C** | **Capability Seam 三段式**(definition / provider / consumer) | 我们的 provider 是直接绑实现(如 `eastmoney`),没有 seam 抽象 | **高** — 用户上轮问"yfinance/eastmoney 可切换" |
| **D** | **Event 三模式**(emit / waterfall / around) | 我们没统一事件模式,retry/circuit-breaker/audit 是散落的代码 | 高 — 一致性 + 插件化 |
| **E** | **Surface event 分类**(4 surface + N log-only) + `SurfaceOp.replace(startSeq, endSeq)` | 我们的 SSE event 没分类,state 是 mutable,无审计 trail | 高 — 审计 + 回放 |
| **F** | **Subagent 6 provider 注册表**(spawn/fork-in-process/ACP/Codex/Claude Code/DSH SDK) | 我们的 data/news/alpha/synthesizer 是写死函数 | 高 — 扩展性 |
| **G** | **Scope 三件套**(`createScope` / `scopeOf` / `scopeTarget`)+ per-agent `restrict_tools` | sub-agent 共享主 agent scope,工具可能污染 | 中-高 — 隔离 |
| **H** | **Agent 6 能力**(followup / steer / inject / send / runMaintenance / whenIdle) | 我们只有 `stream_chat` + `cancel` | 中 — `inject` 对定时任务、`steer` 对中途改主意 |
| **I** | **Hook bridges 5 个生命周期点**(`PreToolUse` / `PostToolUse` / `UserPromptSubmit` / `Stop` / `SessionStart`) | 我们 lifecycle hook 散落在 orchestrator 各处 | 中 — 统一 |
| **J** | **Profile + Bundle 分层组合**(`dsh-base` → `dsh-web-app` → patch) | 我们没有 profile 概念 | 低 — 单 web app 用不到 |

### 11.4 综合借鉴优先级(合并 §5.1 + §10.3.1 + §11.3)

**P0 — 1 周内动(用户痛点 / 反复失败的根因)**

| # | 动作 | 来源 | 价值 | 工作量 |
|---|---|---|---|---|
| **P0-1** | 抽出 `quote_provider` seam(eastmoney/yfinance/akshare/stub 可选) | C | 高(用户上轮诉求) | 0.5d |
| **P0-2** | 引入 PTC 模式(LLM 二选一:TS-like program / JSON tool_calls) | B | **极高**(L3 多工具并发) | 1.5d |
| **P0-3** | Tool 管线补 5 阶段(pre / guards / execute / post / result) | A | 极高(L3 fallback / HITL) | 1d |
| **P0-4** | Surface event 分类(4 surface + N log-only)+ SSE 推送只推 surface | E | 高(前端简化 + 审计) | 0.5d |

**P1 — 月内动(扩展性)**

| # | 动作 | 来源 | 价值 | 工作量 |
|---|---|---|---|---|
| **P1-1** | Subagent named provider 注册表(data/news/alpha/synthesizer) | F | 中(扩展) | 1d |
| **P1-2** | Event 三模式(emit/waterfall/around)统一抽象 | D | 中(一致性) | 1d |
| **P1-3** | Per-agent scope(sub-agent 隔离 tool/LLM/prompt) | G | 中(隔离) | 1d |
| **P1-4** | Agent 加 steer / inject / whenIdle 三能力 | H | 中(定时任务) | 0.5d |

**P2 — 季度内(看需求动)**

| # | 动作 | 来源 | 价值 | 工作量 |
|---|---|---|---|---|
| **P2-1** | Lifecycle hook bridges 统一(PreToolUse/PostToolUse/SessionStart) | I | 中(可观测) | 1d |
| **P2-2** | SurfaceOp.replace 模型(append-only event log) | E | 中(审计回放) | 1d |
| **P2-3** | Profile + Bundle 分层组合 | J | 低(单 web app 不需要) | 2d |

**总预算:约 11.5 天(对比之前的 8 天,加了 P0-3 完整 5 阶段 + P1-2 event 三模式)**

### 11.5 我们不应该搬的(dsh 过度设计部分)

| dsh 设计 | 不要搬的原因 |
|---|---|
| **Cordis 框架本身** | Python 生态差异,dataclass + register 就够 |
| **Branded IDs**(`SessionSeq` 等) | Python 不需要类型层面的强保证 |
| **BrandedNumber + schemastery 校验** | 我们用 Pydantic,功能重叠 |
| **Append-only Session Log 全量** | 没多客户端 / fork / resume 需求 |
| **Declaration merging**(merge-extensible event map)| TS 特性,Python 没等价物 |
| **`ignorable?: true` 严格拒绝重建** | 我们没跨 session resume,直接 fallback 重试 |
| **session/end-seed marker** | 没 fork + crash recovery 复合场景 |
| **AssistantStreamFrame 完整 bracketed** | SSE 推送用 dict 就够 |
| **64KB session.md 全量精读** | 除非真要做多客户端,不必读那么深 |
| **PTC worker-thread backend** | 我们只 web,无沙箱 |
| **ACP / SDK / 多 profile CLI** | 单一 web app |

### 11.6 一句话总结

> **我们做了 60% 的 dsh 设计**(plugin / tool / memory / permission / streaming / tier1 short-circuit / L3 judge),**少了 30% 的关键能力**(5 阶段管线 / PTC / capability seam / scope / event 三模式),**多了 10% 的 Python 化优势**(entry_points / Pydantic / 三层 memory / LLM-judge 解耦)。
>
> **最该学的不是"它们做了什么",而是"它们怎么把 capability 拆成 seam / 怎么用 event 三模式把 cross-cutting 抽出来"**——这俩心智模型能让我们的 harness 从"能跑"变成"可演化"。

### 11.7 立刻可动手的最小动作(再压缩)

如果只能做 **2 天**(2 个工作日),推荐:

1. **Day 1 上午:quote_provider seam**(0.5d)— 直接回应用户上轮诉求
2. **Day 1 下午 - Day 2:PTC 模式骨架**(1.5d)— 写一个 `PTCExecutor`,接受 LLM 返回的 program,内部 `asyncio.gather` 并发调工具,保留现有 JSON tool_calls 路径作为 fallback

完成后:
- 用户能切换 eastmoney ↔ yfinance ↔ akshare
- LLM 可以并发调 quote+fundamentals+news,不再触发"unknown tool ''"错误
- 现有功能完全兼容

要不要这样动?

---

## 十二、TradingAgentsPlus 现状 — Memory 与多 Session

> 在调研完 dsh 后回到我们项目,看我们当前的 memory 和多 session 实现是怎么样。

### 12.1 我们的 Memory 架构(`tradingagents/agent_harness/memory/`,606 行)

#### 12.1.1 三层划分(清晰,dsh 没我们分层干净)

| 层 | 类 | 存储 | Key | TTL | 用途 |
|---|---|---|---|---|---|
| **L1 SESSION** | `SqliteSessionMemory` | `.ta_cache/session_memory.sqlite` | `session:{session_id}:{key}` | 24h | 当前 session 的对话历史 + 短期上下文 |
| **L2 PREFERENCES** | `UserPreferencesMemory` | `.ta_cache/user_prefs.sqlite` | `(user_id, key)` | 无 | 用户偏好设置 |
| **L3 REFERENCES** | `AgentReferencesMemory` | `.ta_cache/agent_refs.sqlite` | `(kind, ref_key)` | 可选 | 跨 session 知识缓存(行情/财务/新闻) |

共同接口 `MemoryLayer`(base.py 58 行):
- `get(key, session_id=None) -> MemoryEntry | None`
- `set(key, value, session_id=None, ttl_seconds=None, metadata=None) -> MemoryEntry`
- `delete(key, session_id=None) -> bool`
- `list(session_id=None, prefix=None) -> list[MemoryEntry]`

`MemoryEntry` 是 Pydantic model:`key / value / scope / session_id / created_at / expires_at / metadata`。

#### 12.1.2 `MemoryManager` 门面(manager.py 78 行)

```python
class MemoryManager:
    def __init__(self, data_dir=None, l1=None, l2=None, l3=None):
        base = data_dir or ".ta_cache"
        self.l1 = l1 or SqliteSessionMemory(db_path=f"{base}/session_memory.sqlite")
        self.l2 = l2 or UserPreferencesMemory(db_path=f"{base}/user_prefs.sqlite")
        self.l3 = l3 or AgentReferencesMemory(db_path=f"{base}/agent_refs.sqlite")

    def get_layer(self, scope: MemoryScope) -> MemoryLayer: ...  # L1/L2/L3 dispatch
    def get(self, key, *, session_id=None, scope=None) -> MemoryEntry | None: ...
    def set(self, key, value, *, session_id=None, ttl_seconds=None, scope=None, metadata=None): ...

    # 便捷方法
    def append_message(self, session_id, role, content) -> MemoryEntry: ...
    def get_history(self, session_id) -> list[dict]: ...

    # 跨层
    def remember_quote(self, symbol, quote) -> MemoryEntry:  # → L3
    def recall_quote(self, symbol) -> MemoryEntry | None:   # ← L3
```

#### 12.1.3 L1 session 细节(173 行)

- 键格式:`session:{session_id}:{key}`(前缀隔离多 session)
- `append_message()` 自动追加到 `key=history`,**只保留最近 200 条**(静默截断,不归档)
- 默认 TTL = 24h,过期条目 read 时删除
- `get_history()` 返回 `[{role, content, ts}, ...]` 列表

#### 12.1.4 L2 preferences 细节(123 行)

- 键格式:`(user_id, key)`,无 TTL
- `user_id` 默认 `"default"`(单用户场景);多用户时 session_id 当 user_id
- `list(session_id=uid, prefix=...)` 拉用户所有偏好

#### 12.1.5 L3 references 细节(148 行)

- 键格式:`kind:ref_key`(如 `quotes:600036.SS`),TTL 可选
- 用 `quote_provider` 时缓存(我们当前没用 — 上轮加)
- `remember_quote/recall_quote` 跨 session 复用 quote 缓存

#### 12.1.6 与 Context 装配集成(`core/context.py`,ContextPriority)

`ContextPriority.assemble()` 自动注入:

```python
# Layer 6 (CHAT) ← L1 history, last 20 messages
history = self.memory.get_history(session_id)[-20:]
if history:
    layers[Layer.CHAT] = {"messages": history}

# Layer 7 (GLOBAL) ← L2 prefs
uid = user_id or session_id or "default"
prefs = self.memory.l2.list(session_id=uid)
if prefs:
    layers[Layer.GLOBAL] = {p.key: p.value for p in prefs}
```

8 层总预算 4000 tokens(L1/EXPLICIT 200, L2/SKILLS 200, L3/TOOLS 800, L4/FILES 300, L5/DASHBOARD 300, L6/CHAT 1500, L7/GLOBAL 200, L8/SEARCH 500)。

### 12.2 我们的多 Session 现状

#### 12.2.1 模型

**session_id 就是个字符串** — 没有 Session 对象、没有 AgentHandle、没有会话生命周期。

```python
# orchestrator.py
@dataclass
class OrchestratorState:
    session_id: str                        # ← 就是个 string
    user_message: str
    intent: Intent = Intent.UNKNOWN
    symbols: list[str] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final: Any = None
    error: str | None = None

# harness.py
async def stream_chat(
    self,
    session_id: str,                       # ← 字符串参数
    user_message: str,
    *,
    history: list | None = None,
) -> AsyncIterator[tuple[str, dict]]:
    async for event in self.orchestrator.stream_chat(
        session_id=session_id,             # ← 透传
        user_message=user_message,
        history=history,
    ):
        yield event
```

#### 12.2.2 多 session 隔离

**靠 SQLite key 前缀**:`session:{session_id}:*`,session A 和 session B 的 history 是不同 row。

- ✅ 多 session 互不污染
- ✅ SQLite 单表 + 索引,简单
- ❌ 没有 session 元数据表(创建时间、最后活跃、关联 user_id 等)
- ❌ 没有"列出所有 session"接口
- ❌ 没有跨 session 查询("哪些 session 看过 600036.SS")

#### 12.2.3 没有的能力

| 能力 | dsh 有 | 我们有吗 |
|---|---|---|
| `Session` 类(创建/恢复/销毁) | ✅ `ctx.sessions.create/resume/dispose` | ❌ |
| `AgentHandle`(disposer 是 capability) | ✅ | ❌ |
| Fork(seed + end-seed marker) | ✅ | ❌ |
| Crash recovery | ✅ `session/end-seed` + `assistant/message.interrupted` | ❌ |
| In-flight session tracking | ✅ `AgentStatus` (idle/running) | ❌ |
| Session 列表 / 跨 session 查询 | ✅ `ctx.sessionQuery` | ❌ |
| Agent 6 能力(followup/steer/inject/send/runMaintenance/whenIdle) | ✅ | ❌ |
| Turn/Step boundary marker | ✅ `turn/start/end`, `step/start/end` | ❌ |
| TurnEndReason | ✅ `completed/cancelled/errored/max-tokens/interrupted` | ❌(只 ok/error) |
| 历史截断归档 | ❌(append-only,只看 projection) | ❌(静默丢 200 之前) |
| TTL per entry | ⚠️(依赖 persistence backend) | ✅ L1 默认 24h,L3 可设 |

### 12.3 Memory 维度 side-by-side

| 维度 | 我们 | dsh | 差距 |
|---|---|---|---|
| **存储抽象** | `MemoryLayer` ABC + 3 SQLite 实例 | `SessionPersistence` seam + JSONL/SQLite backend | 我们更简单;dsh 多 backend 可换 |
| **键模型** | 字符串 `session:{id}:{key}` / `(user_id, key)` / `kind:ref_key` | Branded `SessionId` + Branded `SessionSeq` + SessionEvent type | dsh 类型更强 |
| **数据模型** | `MemoryEntry`(Pydantic) | `SessionEvent<T>`(判别联合) | dsh 编译期保证 |
| **写入语义** | `INSERT OR REPLACE`(可覆盖) | append-only(不可改) | **dsh 强** — 审计 trail |
| **过期** | TTL per entry(L1 默认 24h,L3 可设) | 无 TTL,持久化完整 log,靠 compaction | 我们更省存储 |
| **并发** | threading.Lock | async + sqlite WAL | dsh 更现代 |
| **序列化** | JSON.dumps | lossless JSON(detail 在 dsh-session) | dsh 严格 |
| **恢复** | 无 — SQLite 是 source of truth | `sessions.create(id, { seed })` 回放 | **dsh 强** |
| **跨层关联** | `MemoryManager.remember_quote()` 显式调用 | session log + Remote adapters 隐式 | 一样 |
| **多用户** | `user_id` 字段(默认 "default") | session-scoped + user-scoped Remote | dsh 更灵活 |
| **LLM 注入** | `ContextPriority._inject_memory()` Layer 6/7 | `systemPrompt.section()` + `SessionSurface` projection | 我们直接,dsh 间接 |
| **观测** | `audit.log()` 单点 | `tools/result` 同步 emit + 30+ 事件监听器 | **dsh 强** |

### 12.4 我们做得好的 5 个地方

| # | 我们的强项 | 为什么好 |
|---|---|---|
| **1** | **3 层 Memory 分层(L1/L2/L3)+ 不同 TTL 策略** | dsh 把这些混在 session log,靠 Remote 兜底;我们直接 |
| **2** | **Pydantic MemoryEntry 类型安全** | 自带 key/value/scope/session_id/created_at/expires_at/metadata |
| **3** | **TTL per entry**(L1 24h,L2 无,L3 可设) | dsh append-only 全量存,靠 compaction 清理 |
| **4** | **ContextPriority 自动注入**(Layer 6/7) | 一行 `assemble(session_id=...)` 自动从 L1/L2 拉 |
| **5** | **跨层便捷方法**(`remember_quote/recall_quote`) | L1 历史 + L3 缓存配合用 |

### 12.5 dsh 做得比我们好的 8 个地方

| # | dsh 设计 | 价值 | 我们的差距 |
|---|---|---|---|
| **A** | **Append-only SessionEvent log**(不可覆盖) | **极高** — 完整审计 trail,可回放 | 我们 `INSERT OR REPLACE` 直接覆盖 |
| **B** | **SessionSurface projection + SurfaceFoldReplacement** | 极高 — 同 log 派生不同视图,UI/审计/LLM 各取所需 | 我们没有 projection,L1 history 是 source of truth |
| **C** | **Session 第一公民**(`ctx.sessions.create/resume/dispose`)+ `AgentHandle` disposer | 高 — 完整生命周期,in-flight 可追踪 | 我们 session_id 是裸字符串 |
| **D** | **Fork + end-seed boundary** + crash recovery(`session/end-seed` marker) | 中-高 — 多 session 复用历史 / 服务挂了能恢复 | 我们无 fork / 无 crash recovery |
| **E** | **Session 列表 + 跨 session 查询**(`ctx.sessionQuery`) | 中 — "哪些 session 看过 600036.SS" | 我们没这个 API |
| **F** | **Turn/Step boundary marker** + `TurnEndReasonMap` | 中 — 知道每个 turn 为何结束 | 我们只 ok / error |
| **G** | **Agent 6 能力**(followup/steer/inject/send/runMaintenance/whenIdle) | 中-高 — 定时任务 / 中途改主意 / 等 drain | 我们只有 stream_chat |
| **H** | **30+ 事件监听器全生命周期观测** | 中 — 工具/agent/turn 全事件可观测 | 我们只有 audit.log 单点 |

### 12.6 综合建议(给我们的 memory + 多 session)

**保留的(我们的强项)**
- 3 层 Memory 分层 + 不同 TTL
- Pydantic MemoryEntry
- ContextPriority 自动注入 Layer 6/7
- 跨层便捷方法

**该学的(增量价值)**

| # | 动作 | 价值 | 工作量 | 来源 |
|---|---|---|---|---|
| **P0-M1** | **Append-only event log** — 把 L1 从 K-V 改成 append-only `Event{seq, type, data, time}`,LLM 看到的历史从 log 派生 | 极高(审计 + 回放) | 1d | dsh A+B |
| **P0-M2** | **Session 第一公民** — 加 `Session` dataclass / class,带 `id / created_at / last_active / user_id / status`,in-memory + SQLite 表 | 高(in-flight 追踪) | 0.5d | dsh C |
| **P0-M3** | **Turn/Step boundary + TurnEndReason** — 在 log 里加 `turn/start/end` `step/start/end`,TurnEndReason enum | 中(审计 + 调试) | 0.5d | dsh F |
| **P1-M1** | **Session 列表 + 跨 session 查询** API(`list_sessions(user_id)` / `search_sessions(symbol=...)`) | 中(管理) | 0.5d | dsh E |
| **P1-M2** | **Fork + session/end-seed marker**(可选,从已有 session 派生新 session 复用历史) | 中(实验 / 对比) | 1d | dsh D |
| **P1-M3** | **Agent 6 能力**(`followup` 已有 / 加 `steer` / `inject` / `whenIdle`) | 中-高(定时任务) | 0.5d | dsh G |
| **P2-M1** | **Crash recovery**(`assistant/message.interrupted` marker + 重启时检测) | 中(可靠性) | 1d | dsh D |
| **P2-M2** | **Event 30+ 监听器** 统一抽象(emit/waterfall/around) | 中(可观测) | 1d | dsh H |

**总预算:约 5.5 天**

### 12.7 不该搬的

| dsh 设计 | 不搬的原因 |
|---|---|
| **Branded SessionSeq / SessionLogOffset** | 我们用普通 int,Python 不需要类型层强保证 |
| **完整 lossless JSON 序列化** | 我们 JSON.dumps 已经够 |
| **Merge-extensible SessionEventMap** | TS 特性,Python 没等价物 |
| **`ignorable?: true` 严格拒绝重建** | 我们没跨 session resume 严格需求 |
| **Multiple persistence backend**(jsonl/sqlite) | 我们 SQLite 一份够用 |
| **Compaction 压缩历史** | 我们 L1 history 只保留最近 20 条,直接 truncation |
| **会话恢复 + session header schema_version** | 我们没跨版本升级场景 |

### 12.8 一句话总结

> **我们的 memory 比 dsh 干净**(3 层 + TTL + Pydantic),**多 session 管理比 dsh 简单 50 倍**(裸 session_id 字符串 vs Session 类 + 6 Agent 能力 + 30+ 事件)。
>
> **该学的不是"它们怎么做 memory",而是"append-only event log + surface projection + Session 第一公民"**——这三个心智模型能把我们的可观测性、可审计性、可恢复性从"能用"变成"可运维"。
