# TradingAgentsPlus 分析超时与阶段恢复设计

**日期：** 2026-09-01  
**范围：** Web 分析任务心跳、模型错误分类、阶段报告持久化、失败重试

## 背景

`688836.SS` 的一次分析在四个分析师报告完成后进入研究团队。MiniMax-M3 在空头研究员节点发生读取超时，SDK 重试期间约三分钟没有产生 LangGraph chunk；现有 Worker 只在节点输出或外部请求边界更新心跳，因此 watchdog 先把任务标记为 `heartbeat_timeout`。页面最终只显示“分析超时”，既没有指出模型和失败节点，也没有展示已经完成的四份报告。

这暴露出三个不同问题被混在了一起：

1. Worker/服务是否仍然存活。
2. 当前分析阶段是否持续产生进展。
3. 单次模型或数据源调用是否失败。

本设计将三类信号分离，并让失败任务保留可查看、可复用的阶段成果。

## 目标

- 长模型调用期间不再因为没有 LangGraph chunk 被误判为 Worker 失联。
- 模型超时、限流、认证错误、供应商不可用、数据源失败、Worker 失联和整体任务超时具有不同的稳定错误码和中文提示。
- 每个分析师或决策阶段完成后立即将结果持久化到 SQLite，任务失败后仍可查看。
- 支持从兼容的 LangGraph checkpoint 创建一次新的重试任务，不覆盖原任务的审计记录。
- 保持现有 CLI 分析行为、完整报告目录格式和现有 `/api/runs` 查询兼容。

## 非目标

- 不引入 Redis、Celery、PostgreSQL 或多 Worker 调度。
- 不实现跨机器任务迁移。
- 不保证恢复一个仍在远端供应商执行的 HTTP 请求；重试从最近已提交的图 checkpoint 开始。
- 不把完整报告文件系统迁移进 SQLite。

## 方案选择

### 方案 A：只增大心跳阈值

实现最小，但仍会把模型超时误报为 Worker 超时，而且失败后没有阶段成果。拒绝采用。

### 方案 B：独立 Worker 租约、结构化错误和阶段成果持久化

将执行器存活、分析活动和供应商调用分开记录；失败时保留阶段成果。复杂度适中，可直接解决当前问题。作为第一阶段实施。

### 方案 C：完整持久化任务队列和分布式恢复

扩展性最高，但需要新的队列、锁和并发所有权模型，超出当前本地单用户平台的需求。暂不实施。

## 总体架构

```text
浏览器创建任务
  -> RunManager 写入 web_runs
  -> WorkerLease 在 Worker future 存活期间每 15 秒续租
  -> WebRunRunner 执行 LangGraph
       -> 节点/chunk 更新 activity_at、phase、agent、progress
       -> LLM/Data Provider 调用应用明确 timeout/retry 策略
       -> 阶段完成后 upsert analysis_run_artifacts
       -> LangGraph checkpoint 保存可恢复图状态
  -> 成功：发布完整报告并清理 checkpoint
  -> 失败：保留 artifacts/checkpoint，写入结构化终态
  -> 用户可查看部分报告，或创建一次关联的新重试任务
```

## 第一阶段：可靠终态与部分报告

### 1. 分离租约与活动时间

`web_runs` 增加：

- `worker_heartbeat_at`：执行该任务的 Worker lease 最近续租时间。
- `last_activity_at`：最近一次实际分析活动时间，例如 chunk、阶段变化、报告产出或外部请求边界。
- `active_operation`：安全、可展示的当前操作类型，如 `llm`、`market_data`、`publishing`。
- `active_provider`、`active_model`、`active_attempt`：当前供应商调用诊断信息，不保存提示词、密钥或原始请求。

旧字段 `last_heartbeat_at` 暂时保留为兼容别名，读取时映射到 `worker_heartbeat_at`；新代码不再用它表示分析进展。

Worker future 进入 `running` 后启动一个与该 future 生命周期绑定的 lease 线程，每 15 秒调用 `renew_worker_lease(run_id, owner_token)`。lease 线程不能修改 phase、progress 或 activity，也不能延长任务的固定 `timeout_at`。future 完成、任务进入终态或服务关闭时必须停止并 join 该线程。

每个任务生成不可预测的 `owner_token`，只保存在当前进程内；只有持有 token 的 lease 线程可以续租，避免旧 Worker 对新任务或恢复后的记录写入迟到心跳。

watchdog 规则：

| 条件 | 终态 | `terminal_reason` |
| --- | --- | --- |
| 当前时间达到固定 `timeout_at` | `timed_out` | `run_deadline_exceeded` |
| Worker lease 超过 180 秒未续租 | `interrupted` | `worker_lease_expired` |
| 模型或数据源抛出已分类异常 | `failed` | 对应供应商错误码 |

`last_activity_at` 长时间不更新不会直接终止任务；页面将其显示为“模型处理中，最近活动于 …”。固定的两小时 deadline 仍是阻塞或失控任务的最终上限。

### 2. 模型请求预算和错误分类

Web 运行使用显式供应商策略：

- 单次 LLM 请求 timeout 默认 120 秒。
- SDK `max_retries` 默认 1 次；采用供应商 SDK 的退避策略。
- 单个 LLM 操作的总预算默认 300 秒，不能超过任务剩余 deadline。
- 每次尝试开始前记录 `active_provider`、`active_model`、`active_attempt` 并更新 `last_activity_at`。
- 请求返回或抛错后清理 active operation；失败异常由统一分类器转换为安全的错误结构。

稳定错误码：

| 错误码 | 状态 | 中文提示示例 |
| --- | --- | --- |
| `model_timeout` | `failed` | MiniMax-M3 在空头研究员阶段响应超时 |
| `model_rate_limited` | `failed` | 模型服务请求过于频繁，请稍后重试 |
| `model_auth_error` | `failed` | 模型服务认证失败，请检查配置 |
| `model_unavailable` | `failed` | 模型服务暂时不可用 |
| `data_source_timeout` | `failed` | 行情或研究数据源响应超时 |
| `data_source_unavailable` | `failed` | 必需的数据源不可用 |
| `worker_lease_expired` | `interrupted` | 分析执行器失去连接 |
| `run_deadline_exceeded` | `timed_out` | 分析超过最长运行时间 |
| `worker_error` | `failed` | 分析执行发生未分类错误 |

错误记录增加 `failed_phase`、`failed_agent`、`failed_provider`、`failed_model` 和 `retryable`。`error_message` 只保存安全、用户可读的信息；完整 traceback 写服务日志，不进入 API。API 保留原 `error_code` 字段，并使其与 `terminal_reason` 一致。

可选数据源失败仍按现有降级策略继续执行；只有被分析节点判定为必需且没有可用回退时，才终止任务。

### 3. 阶段成果持久化

新增 `analysis_run_artifacts`：

```sql
CREATE TABLE analysis_run_artifacts (
  run_id TEXT NOT NULL,
  artifact_key TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  phase TEXT NOT NULL,
  agent TEXT,
  title TEXT NOT NULL,
  content_markdown TEXT NOT NULL,
  status TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, artifact_key),
  FOREIGN KEY (run_id) REFERENCES web_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX idx_run_artifacts_sequence
  ON analysis_run_artifacts(run_id, sequence);
```

允许的 `artifact_type` 首期包括：

- `analyst_report`
- `research_debate`
- `risk_assessment`
- `trader_plan`
- `portfolio_decision`
- `executive_summary`

当 chunk 中某项报告首次出现或内容变化时执行幂等 upsert。只有节点完成后才标记 `status=completed`；生成中的内容可保存为 `partial`，但不得冒充完整报告。内容使用 Markdown 原文，页面继续通过统一 Markdown renderer 展示。

新增接口：

- `GET /api/runs/{run_id}/artifacts`：按 `sequence` 返回阶段成果。
- 现有 `GET /api/runs/{run_id}` 增加 `artifact_count`、`completed_artifact_count` 和 `has_partial_results`。

失败、超时和中断任务的详情页显示“已完成内容”和失败位置，不进入正式“报告库”；只有完整发布成功的报告继续进入 `reports` 索引。

### 4. 页面交互

任务详情页终态区域显示：

- 明确的状态标题，例如“模型响应超时”，不再统一显示“分析超时”。
- 失败阶段、智能体、模型、供应商和发生时间。
- 已完成报告数量，以及可展开的阶段报告。
- `retryable=true` 时显示“从失败阶段重试”；不支持恢复时显示“重新分析”。

运行中显示当前操作和最近活动时间。进度只由已完成阶段计算，不因 lease 心跳变化，也不在失败时强制变成 100%。

## 第二阶段：从 checkpoint 创建重试任务

TradingAgents 已有基于 LangGraph `SqliteSaver` 的 checkpoint 能力。Web 运行启用 checkpoint，但需增加运行归属和兼容校验：

- checkpoint signature 包含 ticker、日期、资产类型、分析师集合、研究深度、输出语言、模型供应商、快速模型、深度模型以及图版本。
- checkpoint 元数据记录源 `run_id` 和最后完成节点。
- 完整成功后清理 checkpoint；失败、超时和中断时保留到可配置 TTL，默认 7 天。
- 用户改变任何影响图结构或提示输出的参数时，不允许使用旧 checkpoint。

新增 `POST /api/runs/{run_id}/retry`：

1. 原任务必须为 `failed`、`timed_out` 或 `interrupted`，并且 `retryable=true`。
2. 当前不能存在其他活动任务。
3. 服务验证 checkpoint 存在且 signature 与原请求完全一致。
4. 创建新的 `run_id`，保存 `parent_run_id`、`attempt_number` 和 `resume_checkpoint_id`。
5. 新任务从最近已提交 checkpoint 继续执行；原任务及 artifacts 保持只读。
6. 新任务成功后生成一份正常正式报告，并在运行详情中保留重试链路。

若 checkpoint 不存在、损坏或版本不兼容，接口返回 `409 checkpoint_unavailable`，页面降级为“重新分析”。不得静默从头运行并声称是断点重试。

## 状态和并发约束

- 继续保持本地部署一次只有一个活动任务。
- 所有终态转换使用现有 RunManager 锁下的 compare-and-set 路径，终态不可逆。
- lease、供应商异常和用户取消同时发生时，锁内第一个成功的终态转换获胜，其他迟到更新被忽略。
- 被 watchdog 终止后返回的模型结果不能写 artifacts、发布报告或改变终态。
- retry 创建新任务而非复活旧任务，保证审计记录清晰。
- 服务重启将旧 `queued/running` 任务标记为 `interrupted/service_restarted`；存在兼容 checkpoint 时设置 `retryable=true`。

## 数据迁移与兼容性

- 通过下一版幂等 SQLite migration 增加字段和 `analysis_run_artifacts` 表。
- 旧运行记录的新字段允许 NULL；读取时根据旧 `last_heartbeat_at` 提供兼容值。
- 旧事件、报告目录和 `/api/history` 行为不变。
- 新 API 字段均为附加字段，现有浏览器客户端不会因未知字段失败。
- 不从历史 message 事件自动回填 artifact，避免无法可靠判断 partial/completed；迁移后的新任务开始写入。

## 可观测性

每次供应商操作记录结构化日志：`run_id`、phase、agent、provider、model、attempt、duration_ms、outcome、error_code。不得记录 API key、Authorization、完整提示词或完整模型响应。

设置诊断页增加：

- Worker lease 间隔和过期阈值。
- LLM 单次 timeout、最大重试次数和操作总预算。
- 最近失败任务的错误分类汇总。

## 测试策略

先写失败测试，再实现：

1. fake clock 验证 lease 续租、lease 过期、固定 deadline 和终态竞争。
2. 模拟一个超过 180 秒但 Worker lease 正常续租的模型调用，确保不会误判为超时。
3. 模拟 OpenAI 兼容客户端的 timeout、429、401、5xx，验证错误分类和中文安全消息。
4. 验证迟到模型响应不能覆盖失败/取消/超时终态。
5. 验证每种 chunk 生成幂等 artifact，partial 不冒充 completed，失败后 API 仍可读取。
6. 验证正式报告库只索引 completed 报告。
7. checkpoint signature、成功清理、失败保留、兼容重试和 409 降级测试。
8. 浏览器测试运行中诊断、失败详情、阶段报告展示和重试按钮。
9. 完整运行 Ruff、compileall 和 pytest；真实供应商测试保持显式 opt-in。

## 分阶段验收标准

### 第一阶段

- LLM 调用三分钟无 chunk 但 Worker lease 正常时，任务仍保持运行。
- 模型最终超时时，任务状态为 `failed/model_timeout`，页面显示模型和失败智能体。
- 已完成的四份分析师报告可以在失败任务详情中查看。
- Worker 真正停止续租后进入 `interrupted/worker_lease_expired`。
- 达到两小时固定 deadline 后进入 `timed_out/run_deadline_exceeded`。

### 第二阶段

- 失败任务存在兼容 checkpoint 时可创建新重试任务，并从最后完成节点继续。
- 无 checkpoint 或参数不兼容时明确返回 409，不伪装成恢复。
- 原任务、部分报告和重试链路始终可追溯。

