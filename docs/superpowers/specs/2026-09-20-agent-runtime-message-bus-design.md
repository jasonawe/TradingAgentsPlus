# Harness AgentRuntime 与受监管消息总线设计

日期：2026-09-20

状态：已确认，待实施计划

范围：`tradingagents/agent_harness`

## 1. 背景

当前 Harness 注册了 Planner、Verifier、Synthesizer、Data、News、Alpha 六个 Agent，但主执行链路仍由 `Orchestrator` 集中完成规划、工具执行、校验和答案合成。领域 Agent 名称在计划中主要被转换为其第一个工具，Agent 实例没有通过统一契约真正接收任务，也不存在 Agent 间的任务、结果、质疑、返工或 handoff 消息。

同时，`Orchestrator` 已增长到三千余行，并直接承担以下职责：

- 路由、CRUD 分派和领域参数构造；
- LLM 规划 Prompt、解析和白名单；
- 工具校验、执行、重试、熔断、去重与 HITL；
- L1/L2/L3 校验与修复策略；
- 答案合成、引用重试和 UI 文案渲染；
- Session、Memory、Checkpoint、Audit 和 SSE 生命周期。

这使系统名义上是多 Agent，实际上是集中式工具流水线；也使 Orchestrator 的职责边界难以测试和演进。

## 2. 已确认决策

1. Agent 通过受监管的消息总线协作，不直接持有或调用其他 Agent 对象。
2. 执行模型采用“固定主干、动态修复”：Planner 生成初始 DAG，正常路径固定；信息不足、能力不匹配或验证失败时允许受控的局部补证、返工和 handoff。
3. Agent 消息采用双层可见：完整消息进入持久化 Trace/Inspector/Audit，普通聊天只显示摘要进度。
4. Runtime 为进程内异步调度器，使用 SQLite 持久化任务和消息，不引入 Redis、Celery、NATS 等外部基础设施。
5. 新 Runtime 一次性替换现有 Tier 2/3 主链路，不保留旧 Orchestrator 执行回退或双写路径。
6. Tier 1 保留零 LLM 快速路径，但统一通过新的 `ToolExecutor` 执行。
7. 现有 HTTP API 和主要 SSE 事件保持兼容；内部只保留一条新执行路径。

## 3. 目标与非目标

### 3.1 目标

- 让六个内置 Agent 在生产主链路中通过统一任务协议真实运行。
- 提供可持久化、可恢复、可审计的 Agent 消息和任务状态。
- 支持固定 DAG 的并行执行及受监管的局部动态修复。
- 将业务规划、工具执行、验证、合成、持久化和表现逻辑从 Orchestrator 中移出。
- 保持 Tier 1 延迟优势、HITL 安全边界和现有用户接口。
- 通过架构测试阻止职责重新回流到 Orchestrator。

### 3.2 非目标

- 不实现跨机器 Agent、外部消息 Broker 或独立 Agent 服务部署。
- 不允许 Agent 自由创建无限任务、无限对话或绕过 Runtime 策略。
- 不保存模型内部思维链；仅保存结构化结论、证据、置信度和简短决策理由。
- 不在本次改造中重做聊天 UI；仅增加进度摘要和 Inspector 数据源。
- 不改变真实交易限制，写操作继续受 HITL 和权限系统控制。

## 4. 总体架构

```text
Harness
  └─ TurnCoordinator（保留 Orchestrator 公共入口名）
       ├─ Router
       ├─ AgentRuntime
       │    ├─ TaskScheduler
       │    ├─ MessageBus
       │    ├─ PolicyGuard
       │    └─ AgentDispatcher
       ├─ MessageStore（SQLite）
       ├─ MessageIngestor
       ├─ ToolExecutor
       ├─ LLMExecutor
       └─ TraceProjector
```

### 4.1 Harness

Harness 继续负责组装依赖和提供公共入口。它创建共享的 AgentRegistry、ToolRegistry、LLMFactory、AgentRuntime、MessageStore、ToolExecutor 和 TurnCoordinator。Harness 不实现执行策略。

### 4.2 TurnCoordinator

现有 `Orchestrator` 的公共类名可以保留，以避免外部 import 破坏；其实现收缩为 TurnCoordinator，职责仅为：

- 接收一轮用户请求；
- 调用 Router 获得 `RouteDecision`；
- Tier 1 调用 ToolExecutor 快速路径；
- Tier 2/3 创建并运行 AgentRuntime run；
- 转发高层生命周期事件；
- 处理用户取消、恢复和最终结束状态。

TurnCoordinator 不直接调用 LLM Provider、具体工具、具体 Agent、Memory 后端或领域参数构造函数。

为保持已发布的 Python 表面兼容，`Harness.orchestrator` 仍返回这个精简对象；它继续暴露 `stream_chat()`、`resume()`、`lifecycle`、`control_bus` 和 `plan_cache` facade。`_plan`、`_execute`、`_execute_ptc`、`_observe`、`_verify`、`_synthesize`、`_call_tool` 等私有方法不保留，仓库内现有调用者必须在同一个切换提交中迁移到下文定义的服务接口。

### 4.3 Router 与 CommandResolver

Router 负责 intent、op、symbol、slot 和 tier 分类，返回完整的 `RouteDecision`。CRUD 的 note、alert、watchlist、scheduled、run、report 参数由独立 `CommandResolver` 负责。它们可以使用现有 `tier.py` 的提取逻辑，但不把领域规则带回 TurnCoordinator。

```python
class RouteDecision(BaseModel):
    intent: Intent
    op: Op
    tier: Literal[1, 2, 3]
    symbols: list[str]
    carry_symbols: list[str]
    slots: dict
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: str
    route_kind: Literal["DIRECT_READ", "SYSTEM_COMMAND", "AGENT_ANALYSIS"]
```

CRUD 不经过 Planner 或领域 Agent。只读 CRUD 走 Tier 1 `CommandResolver → ToolExecutor → ResultFormatter`；需要 HITL、等待或崩溃恢复的写 CRUD 创建一个 Runtime command run，其中唯一业务节点是 `SYSTEM_COMMAND` task，由 `CommandExecutor` 按 CommandSpec 调用 ToolExecutor。CommandExecutor 是确定性系统 handler，不是 LLM Agent，也不能创建动态任务。

### 4.4 AgentRuntime

AgentRuntime 是 Tier 2/3 的唯一执行引擎。它负责持久化并调度 PlanGraph、投递消息、维护依赖、控制并发、应用动态修复策略以及决定 run 是否可以结束。Runtime 不编写规划或答案内容，也不实现具体领域分析。

### 4.5 ToolExecutor

ToolExecutor 是所有 Agent 和 Tier 1 共用的工具执行入口，统一处理：

- ToolRegistry 查询和 Pydantic 参数校验；
- AgentScope 工具权限；
- ToolPipeline hooks；
- HITL approval；
- retry、timeout、circuit breaker；
- 读写幂等和短窗口去重；
- 结果标准化和错误分类。

Agent 不直接持有 ToolRegistry，不直接调用 `tool.invoke()`。

### 4.6 LLMExecutor

LLMExecutor 是所有 Agent 共用的 LLM 调用边界，统一处理 provider 选择、timeout、retry、circuit breaker、响应缓存、token 预留与结算以及结构化错误分类。Agent 不直接持有 LLMFactory，不直接调用 provider。并发 Agent 在调用前通过 UsageLedger 原子预留预算，调用完成后按真实 usage 结算；预留失败时任务以 `BUDGET_EXHAUSTED` 结束，不再发起 provider 请求。

### 4.7 TraceProjector

TraceProjector 从持久化消息生成两个视图：

- 用户视图：简短进度和现有高层 SSE 事件；
- Inspector/Audit 视图：完整任务、消息、证据引用、耗时、token、返工原因和状态转换。

EventBus 继续服务插件和横切 hook，不承载 Agent 任务消息，也不是消息存储。

### 4.8 MessageIngestor

AgentChannel、ToolExecutor 和 Runtime 状态机产生的内容必须先经过 MessageIngestor，之后才能写入 MessageStore。Ingestor 对所有 dict/list 递归遍历，最大深度 12、最大节点数 10,000；key 使用大小写无关 denylist（`reasoning`、`chain_of_thought`、`scratchpad`、`hidden_prompt`、`authorization`、`api_key`、`token`、`secret` 及 Tool schema 的 sensitive_fields），命中推理字段则拒绝整条消息，命中 secret 字段则用类型化 REDACTED sentinel 替换。字符串中的 `<scratchpad>`、`<chain_of_thought>` 等结构化标记也拒绝。随后执行 Pydantic schema 校验、字段长度限制和 canonicalization。TraceProjector 只处理已经安全落库的数据和视图级摘要，不承担“落库前脱敏”。无法通过语义分析保证普通 prose 从未包含推理痕迹，因此 Agent Prompt 还必须要求只输出审计摘要；验收标准针对可执行的 schema、key 和 marker 规则。

## 5. Agent 契约

### 5.1 输入与执行上下文

```python
class AgentTask(BaseModel):
    task_id: str
    run_id: str
    turn_id: str
    parent_task_id: str | None
    sender: str
    recipient: str
    objective: str
    inputs: dict
    dependency_results: list[EvidenceRef]
    constraints: TaskConstraints
    required: bool = True
    execution_attempt: int = 1


class AgentExecutionContext:
    session_id: str
    trace_id: str
    scope: AgentScope
    tool_executor: ToolExecutor
    llm_executor: LLMExecutor
    channel: AgentChannel
```

Agent 通过统一入口执行：

```python
async def run(
    self,
    task: AgentTask,
    *,
    context: AgentExecutionContext,
) -> AgentReply:
    ...
```

`AgentChannel` 只允许向 Runtime 提交消息；它不暴露其他 Agent 对象。Runtime 在持久化和 PolicyGuard 审批后才会投递。

### 5.2 AgentReply

```python
class AgentReply(BaseModel):
    success: bool
    content: str
    structured_data: dict | None
    evidence: list[EvidenceRef]
    confidence: float | None
    missing_items: list[str]
    outgoing: list[AgentMessageDraft]
    errors: list[AgentError]
```

`outgoing` 与任务最终状态在同一事务中保存，避免 Agent 成功但消息丢失。流式 `PROGRESS` 通过 AgentChannel 发送，每条消息先持久化再投影到 SSE。

### 5.3 内置 Agent 职责

| Agent | 职责 | 禁止承担 |
|---|---|---|
| PlannerAgent | 仅生成初始 PlanGraph | 后续改图、批准图变更、调工具、直接执行领域任务 |
| DataAgent | 行情、历史、基本面取数与数据摘要 | 新闻、Alpha、最终投资结论 |
| NewsAgent | 新闻取数、时效检查和情绪主题 | 行情和基本面 |
| AlphaAgent | 因子计算、评价和量化解释 | 新闻和 CRUD |
| VerifierAgent | 结构、语义、引用、claim audit；生成 repair | 直接篡改领域结果 |
| SynthesizerAgent | 只基于已验证证据生成最终回答 | 创建新任务、补数据、绕过校验 |

## 6. 消息协议

### 6.1 不可变消息信封

```python
class AgentMessage(BaseModel):
    message_id: str
    seq: int
    run_id: str
    turn_id: str
    task_id: str
    parent_task_id: str | None
    sender: str
    recipient: str
    type: AgentMessageType
    payload: AgentPayload
    evidence_refs: list[EvidenceRef]
    causation_id: str | None
    correlation_id: str
    idempotency_key: str
    execution_attempt: int
    created_at: datetime
```

### 6.2 消息类型

| 类型 | 用途 | 谁可发起 |
|---|---|---|
| `TASK` | 分派任务 | 仅 Runtime；逻辑 sender 可记录为 Planner |
| `RESULT` | 返回结构化结果和证据 | 所有执行 Agent |
| `QUESTION` | 请求任务所需信息 | 所有 Agent |
| `ANSWER` | 回答一个已持久化 QUESTION | Runtime 代表用户或被询问 Agent |
| `HANDOFF_REQUEST` | 申请转交责任 | 领域 Agent、Verifier |
| `REPAIR_REQUEST` | 要求补证或修正 | Verifier |
| `CANCEL` | 取消任务 | Runtime、用户控制入口 |
| `PROGRESS` | 进度摘要 | 所有执行 Agent |

Agent 不能自行将 `HANDOFF_REQUEST` 或 `REPAIR_REQUEST` 变成新任务，也不能提交 raw GraphPatch。只有 Runtime 能把这两类已持久化消息转换为 GraphPatch，并在 PolicyGuard 通过后原子修改 DAG。

### 6.3 可见性与数据最小化

所有消息进入 MessageStore。普通 UI 只接收 TraceProjector 生成的摘要；Inspector 和 Audit 可以读取完整消息。消息不得包含隐藏思维链、草稿推理或模型内部 token；允许保存：

- 结构化结论；
- 证据引用；
- 置信度；
- 缺失项；
- 简短、面向审计的决策理由。

## 7. PlanGraph 与任务状态

### 7.1 PlanGraph

PlannerAgent 输出：

```python
class PlanTask(BaseModel):
    task_key: str
    agent: str
    capability: str
    objective: str
    inputs: dict
    depends_on: list[str]  # planner-local task_key
    required: bool = True
    dependency_mode: Literal["ON_SUCCESS", "ON_TERMINAL"] = "ON_SUCCESS"
    timeout_seconds: float | None = None


class PlanGraph(BaseModel):
    domain_tasks: list[PlanTask]
    budgets: RunBudgets
```

Planner 只规划领域任务。`task_key` 只在单个 PlanGraph 内唯一；Runtime 持久化前将它映射为全局 UUIDv7 task_id，并重写依赖。Runtime 强制附加 `evidence_verifier → synthesizer → answer_verifier` 固定尾部，Planner 无权删除或绕过验证和合成。Runtime 在运行前验证 Agent 注册、capability、依赖存在性、无环、预算和工具权限；无效图不会部分执行。

### 7.2 Task 状态

```text
PLANNED → READY → RUNNING
                  ├─ WAITING_MESSAGE
                  ├─ WAITING_CHILD
                  ├─ WAITING_APPROVAL
                  ├─ SUCCEEDED
                  ├─ FAILED
                  ├─ CANCELLED
                  └─ INDETERMINATE
```

### 7.3 Run 状态

活动状态：`PLANNING`、`RUNNING`、`WAITING_USER`、`WAITING_APPROVAL`、`NEEDS_RECONCILIATION`。

终态：`SUCCEEDED`、`PARTIAL_SUCCESS`、`FAILED`、`CANCELLED`、`LEGACY_INTERRUPTED`。`NEEDS_RECONCILIATION` 是必须人工处理的非自动恢复状态。

`PARTIAL_SUCCESS` 只允许在失败任务由 Planner 明确标记为 `required=False` 时产生；Runtime 不猜测任务是否可选。

## 8. 持久化与投递语义

MessageStore 使用专用的 `{data_dir}/agent_runtime.sqlite`，Web 和 standalone Harness 使用同一 `AgentRuntimeStore` 初始化器及同一组包内版本化 migration。它不依赖 `web/migrations`，也不与 L1/L2/L3、EventLog、plan cache 或 `web_runs` 共用事务。Runtime DB 是 Agent 任务、消息、operation 和 approval 的唯一事实源；向 SessionStore、EventLog 和 Audit 的同步由持久化 outbox 异步投影。

### 8.1 `agent_runs`

- `run_id` 主键；
- `session_id`、`turn_id`；
- `run_kind` 为 `AGENT_ANALYSIS` 或 `SYSTEM_COMMAND`，以及 route、预算、状态、最终结果；
- `worker_id`、lease 和 heartbeat；
- `next_seq`、`terminal_seq nullable`、`graph_revision`；
- `session_projection_state` 为 `PENDING/DELIVERED/FAILED`，以及 `session_projected_at nullable`；
- 创建、更新时间及结束原因。

同一 session 最多一个活动 run。SQLite 使用 partial unique index 约束状态属于 `PLANNING/RUNNING/WAITING_USER/WAITING_APPROVAL/NEEDS_RECONCILIATION` 时的 session_id；新普通 turn 在活动 run 存在时返回 busy，只有显式 ANSWER、confirm、cancel 或 resume 可以操作该 run。

### 8.2 `agent_tasks`

- `task_id` 为 Runtime 生成的全局 UUIDv7 主键，外键指向 run；
- `kind` 为 `AGENT` 或 `SYSTEM_COMMAND`，以及 parent、agent/system_handler、capability、objective、inputs；
- required、state、execution_attempt、resume_count、repair_count、handoff_depth 和最大 execution attempt；
- lease、heartbeat、result、error；

任务依赖存入 `agent_task_dependencies`，唯一约束 `(run_id, task_id, depends_on_task_id)`。

内部 child 等待单独存入 `agent_task_waits`：

```text
wait_id PK
run_id
waiter_task_id FK agent_tasks
child_task_id FK agent_tasks
wait_kind                 # HANDOFF / REPAIR / AGENT_QUESTION
failure_policy            # FAIL_WAITER / RESUME_WITH_FAILURE
state                     # ACTIVE / RESOLVED / CANCELLED
created_at, resolved_at
UNIQUE(waiter_task_id, child_task_id)
```

TaskScheduler 只根据 ACTIVE wait 判断 WAITING_CHILD，重启后用 child 当前状态按 failure_policy 原子解析 wait，并生成 ANSWER/失败引用。

### 8.3 `agent_messages`

- `message_id` 主键；
- `seq` 为 run 内单调序号，唯一约束 `(run_id, seq)`；
- 完整不可变消息信封；
- payload 和 evidence_refs 使用 JSON；
- 唯一约束 `idempotency_key`；
- 按 `(run_id, created_at)`、`(task_id, created_at)` 建索引。

`agent_artifacts` 保存结构化证据正文和内容 hash；消息只携带 EvidenceRef。`runtime_events` 保存所有可重放状态/SSE 事件及单调 seq。`agent_outbox` 保存待投递消息和跨库投影。`agent_operations` 保存工具 operation、HITL approval 和副作用状态。`agent_usage_reservations` 保存 LLM token 预算预留。完整 schema 和状态见第 19、21、27 节。

### 8.4 投递语义

消息采用 at-least-once delivery 加幂等键：

1. 在同一 Runtime SQLite 事务中写入消息、outbox 和任务状态；
2. 提交后将任务放入进程内调度队列；
3. worker claim 任务时写入 lease；
4. 重启后扫描 `READY`、`WAITING_MESSAGE`、`WAITING_CHILD` 和 lease 过期的 `RUNNING`；等待态只有在对应 ANSWER/child RESULT 已持久化时才转 READY；
5. Agent、Runtime 和 ToolExecutor 使用稳定 idempotency key 防止重复副作用。

V1 支持单个活跃 AgentRuntime worker。数据库中的 lease、唯一约束和写工具幂等仍必须实现，以保证崩溃恢复；多进程并发抢占不是本次目标。

## 9. 正常执行流程

```text
Router
  ↓
PlannerAgent → PlanGraph
  ↓
AgentRuntime 校验并持久化 DAG
  ↓
DataAgent / NewsAgent / AlphaAgent 并行执行
  ↓ RESULT + evidence
VerifierAgent（evidence phase）
  ↓ verified evidence
SynthesizerAgent
  ↓ draft answer
VerifierAgent（answer phase）
  ↓
最终回答
```

1. TurnCoordinator 创建 run，并向 PlannerAgent 投递根 `TASK`。
2. PlannerAgent 返回 PlanGraph；Runtime 全量验证后原子持久化任务和依赖。
3. TaskScheduler 并行投递所有 READY 节点。
4. 领域 Agent 通过 ToolExecutor 获取数据，返回 RESULT 和 evidence。
5. 所有必需依赖成功后，VerifierAgent 执行 evidence phase，并签发 `VerifiedEvidenceRef`。
6. SynthesizerAgent 只能读取 Runtime 注入的 VerifiedEvidenceRef，生成 draft answer。
7. VerifierAgent 执行 answer phase，检查 groundedness、citation 和 claim audit；失败时只修复 Synthesizer 或责任领域分支。
8. answer phase 通过后，TraceProjector 才生成 `agent_final` 和双层 trace。

Tier 1 使用 `Router → ToolExecutor → ResultFormatter`，不创建 Agent run，不调用 LLM。

CRUD write 使用 `Router → CommandResolver → AgentRuntime(SYSTEM_COMMAND) → ToolExecutor`。它不调用 Planner、Verifier 或 Synthesizer；操作结果由 ResultFormatter 生成确定性确认文案。这样写命令获得持久化、HITL、取消和恢复能力，同时不伪造不必要的 Agent 协作。

## 10. 动态修复

正常主干固定。只有以下情况允许动态消息修改局部执行图：

- 缺少必要证据；
- Agent 能力不匹配；
- Verifier 判定结果不完整或不可信；
- 用户在等待点补充了约束。

动态策略硬限制：

- 每个任务最多返工 1 次；
- handoff 最大深度 2；
- 每个 run 最多动态新增 6 个任务；
- 每个 turn 最多 20 条 Agent 消息，不含 Runtime 自身状态事件；
- 相同 sender、recipient、task、type、causation 链禁止循环；
- Planner、Verifier 和领域 Agent 都只能申请图变更；只有 Runtime/PolicyGuard 可以批准；
- 写工具始终经过 HITL，消息授权不能绕过 ToolExecutor。

### 10.1 Repair

Verifier 返回 `REPAIR_REQUEST`，包含责任 task、具体缺失证据和验收条件。责任领域 task 保持 SUCCEEDED，原 artifact 被 Verifier 标记为 rejected-for-this-verification。Verifier task 从 RUNNING 进入 WAITING_CHILD。Runtime 将请求转换为带 expected graph revision 的 GraphPatch，经 PolicyGuard 后创建新的 repair child task，并记录 `repair_of_task_id`；child 终结后恢复同一个 Verifier task。必需证据 repair 使用 `FAIL_WAITER`，可选证据 repair 使用 `RESUME_WITH_FAILURE`，因此可选 child 失败不会错误地使 Verifier 失败。修复成功后只重新运行受影响的 Verifier 和 Synthesizer，不重跑无关成功分支。Patch 被拒绝或限制耗尽时，必需任务导致 run FAILED，可选任务记录失败并允许 PARTIAL_SUCCESS。

### 10.2 Handoff

Agent 发出 `HANDOFF_REQUEST` 时只声明需要的 capability 和原因。发起 task 从 RUNNING 进入 WAITING_CHILD。PolicyGuard 检查深度、预算、循环和候选 AgentScope；Runtime 从 AgentRegistry 创建 handoff child。child 结果作为 ANSWER 返回 parent，parent 恢复 READY 并决定如何完成；parent 的 required/optional 属性不变。

### 10.3 Question

可由现有证据回答的问题交给 Planner 或指定 Agent。必须由用户回答时，run 进入 `WAITING_USER` 并发出高层 `waiting_user` SSE。用户回复通过同一 correlation_id 恢复原任务。

## 11. 错误、取消与恢复

### 11.1 错误分类

| 类型 | 处理 |
|---|---|
| 短暂 Tool Provider/网络错误 | ToolExecutor 内部重试，不创建 Agent repair |
| 短暂 LLM Provider/网络错误 | LLMExecutor 内部重试，不创建 Agent repair |
| AgentReply schema 错误 | 当前任务重试一次，仍失败则终止该分支 |
| 证据不足、结论不可信 | Verifier 触发 REPAIR_REQUEST |
| 用户信息或审批缺失 | Run 进入 WAITING_USER/WAITING_APPROVAL |
| Runtime、数据库或策略错误 | Run FAILED，保留完整 trace |

### 11.2 取消

用户取消写入持久化 `CANCEL` 消息，Runtime 将未开始任务标记 CANCELLED，并对运行中协程发出协作式取消。下游节点不再释放。已经完成的工具结果保留用于审计，但不继续合成最终投资回答。

### 11.3 崩溃恢复

- 消息、outbox 和任务状态原子提交；
- RUNNING 任务使用 lease 和 heartbeat；
- 重启后 lease 过期任务回到 READY；
- SUCCEEDED 任务不重跑；
- 工具使用 `task_id + execution_attempt + operation` 幂等键；
- 写操作同时要求持久化 approval id 和 operation id；无法确认远端副作用是否成功时，operation 和 task 进入 `INDETERMINATE`，run 进入 `NEEDS_RECONCILIATION`，不得自动重试。

### 11.4 一次性迁移

- 保留现有聊天历史和 session metadata；
- 不转换旧 Orchestrator checkpoint 为新 PlanGraph；
- Runtime migration 不修改 `harness_checkpoints`。`Harness.set_checkpoint_store()` 完成 legacy store 注入后触发一次扫描：先用 `PRAGMA table_info` 检测是否存在 `workflow_name`；存在时按 `(session_id, workflow_name)`，不存在时按 `session_id` 分组，并以 `updated_at DESC, milestone_id DESC` 选择最新 milestone。扫描结果写入 `agent_legacy_interruptions`；
- legacy migration key 为 `sha256(store_identity|session_id|workflow_or_default|milestone_id)`，表上有唯一约束；每次 `set_checkpoint_store()` 可安全重扫，重复 key 为 no-op；
- 用户恢复旧 turn 时，如果 `state_json.user_message` 存在则创建新 run，并在新 run 中记录来源 milestone；否则返回可操作错误，要求重新发送原问题；
- 该迁移只处理 `harness_checkpoints`，不修改 `web_runs`、分析任务 checkpoint 或 LangGraph L1 文件；
- 已完成历史回答不变；
- Tier 2/3 不保留旧链路 fallback。

Legacy resume 在一个 Runtime DB `BEGIN IMMEDIATE` 事务中读取 interruption：若 replacement_run_id 已存在，直接返回该 run（无论仍活动或已终结），绝不再创建；若为空，则创建 replacement run，并用 `UPDATE ... WHERE replacement_run_id IS NULL` CAS 写入。CAS 失败时回滚本次候选 run 并读取赢家 replacement_run_id。重复 resume 因此始终指向同一 replacement run；只有显式“重新运行”chat 请求才创建新 turn。

## 12. SSE 与双层可见

为了保持现有 Web 客户端兼容，TraceProjector 继续输出：

- `plan_started`、`plan_ready`、`plan_ready_ptc`；
- `tool_call`、`tool_result`；
- `verified`、`answer_verified`；
- `agent_final`、`error`、`usage_summary`。

新增摘要事件：

- `agent_progress`；
- `repair_started`；
- `handoff_requested`；
- `waiting_user`；
- `run_recovered`。

摘要事件不包含完整 Prompt、隐藏推理或敏感工具参数。Inspector 通过独立只读 API 按 run_id 查询任务图和完整结构化消息。

## 13. 代码组织

建议新增：

```text
tradingagents/agent_harness/runtime/
  __init__.py
  models.py
  store.py
  bus.py
  scheduler.py
  policy.py
  dispatcher.py
  projector.py
  runtime.py

tradingagents/agent_harness/core/
  turn_coordinator.py
  command_resolver.py
  tool_executor.py
  llm_executor.py
  turn_repository.py
```

现有 `core/orchestrator.py` 保留公共兼容导出，内部委托 TurnCoordinator，或直接缩减为 TurnCoordinator 实现。以下逻辑必须迁出：

| 当前实现 | 新 owner |
|---|---|
| CRUD 参数构造与 dispatch | Router / CommandResolver |
| `_llm_plan`、Prompt、解析 | PlannerAgent |
| `_execute`、`_execute_ptc` | ToolExecutor / AgentRuntime |
| `_verify`、L3 judge | VerifierAgent |
| `_llm_synthesize` | SynthesizerAgent |
| friendly summary | TraceProjector / ResultFormatter |
| L1/L2/L3 直接读写 | TurnRepository / ContextAssembler |
| checkpoint 细节 | MessageStore / AgentRuntime |

## 14. 测试策略

### 14.1 消息协议

- schema、消息关联和非法 recipient；
- idempotency key 唯一性；
- 消息、深度和预算限制；
- 禁止保存内部思维链字段。

### 14.2 Runtime 状态机

- 固定 DAG 并行和依赖释放；
- 局部失败和 required/optional 语义；
- repair、handoff、循环拒绝和预算耗尽；
- WAITING_USER、HITL、取消传播；
- lease 过期和崩溃恢复。
- child task 完成后、wait 解析前崩溃，恢复后按持久化 failure_policy 只解析一次；
- L1 append 后、Runtime projection DELIVERED 前崩溃，恢复后 projection_key 防重复；
- legacy replacement run 创建后、resume 响应前崩溃，重复 resume 返回同一 replacement_run_id。

### 14.3 Agent 合约

每个注册 Agent 必须证明：

- 能接收 AgentTask 并返回 AgentReply；
- 只能使用 AgentScope 允许工具；
- 不直接引用其他 Agent；
- RESULT 包含证据和置信度；
- Planner、Verifier、Synthesizer 在主链路实际被调用。

### 14.4 端到端

- 单标的行情与基本面分析；
- CRUD read 走 Tier 1，CRUD write 走 SYSTEM_COMMAND + HITL，二者均不调用 Planner LLM；
- Data、News、Alpha 并行；
- Verifier 要求 NewsAgent 补证后成功；
- handoff 批准和策略拒绝；
- HITL 暂停、批准、拒绝和恢复；
- 进程重启后继续未完成任务；
- 用户取消；
- 可选数据源失败产生 PARTIAL_SUCCESS；
- 双层可见；
- Tier 1 保持零 LLM 快速路径；
- 现有 HTTP 和主要 SSE 契约兼容。

### 14.5 架构约束测试

使用 AST/import 测试防止职责重新回流：

- Orchestrator 不直接调用 LLM provider；
- Orchestrator 不包含 Agent 到工具的硬编码映射；
- Orchestrator 不构造 note、alert、scheduled 等领域参数；
- Orchestrator 不直接调用具体工具；
- 所有 `AGENT` task 必须通过 AgentRegistry 的统一执行入口分派；`SYSTEM_COMMAND` 只能进入 CommandExecutor；
- EventBus 不得作为 Agent 消息存储；
- Agent 消息必须先持久化再调度。

## 15. 验收标准

- TurnCoordinator 满足第 25.2 节的 600 个非空非注释行硬上限。
- Tier 2/3 只存在新 AgentRuntime 主链路。
- 六个内置 Agent 均由端到端测试证明真实参与。
- 不再存在 Agent 名到“第一个工具”的降级映射。
- 修复只重跑受影响分支。
- 崩溃恢复不重复已成功的任务或写操作。
- Tier 1 无 LLM，并满足第 25.2 节的 5ms/1.10 倍 benchmark 阈值。
- 完整 Agent 消息可审计，普通 UI 只显示摘要。
- 第 24 节列出的 HTTP 接口和 SSE schema、顺序、重放兼容测试全部通过。
- Harness 相关测试全绿，不保留已知失败。

## 16. 实施顺序约束

虽然部署采用一次性切换，代码实施仍应按可验证的内部依赖顺序完成：

1. 定义消息、任务、PlanGraph 和状态模型；
2. 实现 SQLite MessageStore 和 migration；
3. 实现 ToolExecutor；
4. 实现 AgentRuntime、PolicyGuard 和 Dispatcher；
5. 迁移并接通六个 Agent；
6. 实现 TraceProjector 和恢复逻辑；
7. 用精简 TurnCoordinator 一次性替换 Tier 2/3 主链路；
8. 删除旧 Orchestrator 规划、执行、验证和合成代码；
9. 完成架构约束和端到端回归。

实现过程中不得临时保留第二条生产执行路径。允许在测试中使用 fake Runtime 或 fake Agent，但生产 `stream_chat` 在切换提交中必须只有新主链路。

## 17. 核心协议类型

本节类型为规范性契约。所有 Pydantic model 使用 `extra="forbid"`，避免任意 dict 绕过数据最小化和 provenance 校验。

### 17.1 Evidence 与 Artifact

```python
class EvidenceRef(BaseModel):
    artifact_id: str
    producer_task_id: str
    source_type: Literal["tool", "agent", "user"]
    source_name: str
    content_sha256: str
    as_of: datetime | None


class VerifiedEvidenceRef(EvidenceRef):
    verification_task_id: str
    verification_level: Literal["L1", "L2", "L3"]
    verified_at: datetime
```

正文存入 `agent_artifacts`，EvidenceRef 只引用 immutable artifact 和 hash。Runtime 仅能根据 evidence-phase Verifier 的成功 RESULT 创建 VerifiedEvidenceRef；Agent 不能自行声明 evidence 已验证。SynthesizerAgent 的输入 schema 只接受 VerifiedEvidenceRef。

### 17.2 约束、预算与错误

```python
class TaskConstraints(BaseModel):
    timeout_seconds: float
    max_execution_attempts: int
    allowed_tools: list[str]
    allow_handoff: bool = True


class RunBudgets(BaseModel):
    max_dynamic_tasks: int = 6
    max_messages: int = 20
    max_handoff_depth: int = 2
    max_repairs_per_task: int = 1
    max_total_tokens: int
    deadline_at: datetime


class AgentError(BaseModel):
    code: str
    category: Literal[
        "VALIDATION", "PROVIDER", "TOOL", "POLICY",
        "BUDGET", "TIMEOUT", "CANCELLED", "INTERNAL"
    ]
    retryable: bool
    summary: str
```

### 17.3 Message Draft 与 payload

Agent 返回 `AgentMessageDraft`，不填写 message_id、execution_attempt、causation_id、correlation_id 或 idempotency_key；这些字段由 Runtime 生成。AgentMessage.execution_attempt 始终复制其 task 当前的 `agent_tasks.execution_attempt`；READY 被 worker claim 时才递增 execution_attempt，WAITING_* 恢复只递增 resume_count，不改变 execution_attempt。消息和 operation 幂等键统一使用该字段。

```python
class TaskPayload(BaseModel):
    kind: Literal["TASK"]
    task: AgentTask


class ResultPayload(BaseModel):
    kind: Literal["RESULT"]
    success: bool
    content: str
    artifact_refs: list[EvidenceRef]
    confidence: float | None
    missing_items: list[str]
    errors: list[AgentError]


class AnswerPayload(BaseModel):
    kind: Literal["ANSWER"]
    question_message_id: str
    value: dict | str
    answered_by: str


class CancelPayload(BaseModel):
    kind: Literal["CANCEL"]
    reason_code: str
    summary: str
    requested_by: str


class QuestionPayload(BaseModel):
    kind: Literal["QUESTION"]
    question: str
    answer_schema: dict
    user_required: bool
    expires_at: datetime


class HandoffPayload(BaseModel):
    kind: Literal["HANDOFF_REQUEST"]
    required_capability: str
    objective: str
    evidence_refs: list[EvidenceRef]
    acceptance_criteria: list[str]
    excluded_agents: list[str] = []
    on_reject: Literal["FAIL_REQUESTER", "RESUME_REQUESTER"]


class RepairPayload(BaseModel):
    kind: Literal["REPAIR_REQUEST"]
    target_task_id: str
    repair_kind: Literal["DOMAIN_EVIDENCE", "SYNTHESIS"]
    missing_evidence: list[str]
    acceptance_criteria: list[str]
    rejected_artifact_ids: list[str]
    on_reject: Literal["FAIL_REQUESTER", "RESUME_REQUESTER"]


class ProgressPayload(BaseModel):
    kind: Literal["PROGRESS"]
    stage: str
    summary: str
    percent: int | None = Field(default=None, ge=0, le=100)


AgentPayload = Annotated[
    TaskPayload | ResultPayload | AnswerPayload | CancelPayload |
    QuestionPayload | HandoffPayload | RepairPayload | ProgressPayload,
    Field(discriminator="kind"),
]


class AgentMessageDraft(BaseModel):
    recipient: str
    type: Literal[
        "QUESTION", "HANDOFF_REQUEST", "REPAIR_REQUEST", "PROGRESS"
    ]
    payload: QuestionPayload | HandoffPayload | RepairPayload | ProgressPayload
    evidence_refs: list[EvidenceRef] = []
    reason_summary: str | None = None
```

`AgentReply` 本身由 Runtime 转换成唯一一条 RESULT message；Agent 不得在 `outgoing` 中再创建 RESULT。MessageIngestor 强制 `AgentMessage.type == payload.kind`。各 payload 是 discriminated union；即使 `inputs`、`value` 或 `answer_schema` 内含嵌套 JSON，也必须通过第 4.8 节的递归 key/marker/secret 检查。`reason_summary` 最大 500 字符。

## 18. AgentRegistry V2 与插件迁移

### 18.1 AgentDescriptor

```python
class AgentDescriptor(BaseModel):
    name: str
    contract_version: Literal[2]
    capabilities: list[str]
    priority: int = 0
    allowed_tools: list[str]
    accepts_message_types: list[AgentMessageType]
```

Registry 注册对象为 `(descriptor, factory)`。内置六 Agent 全部升级到 contract version 2。Runtime 通过 `descriptor.capabilities` 验证 PlanTask，不从 Agent 的第一个工具推断 capability。

### 18.2 第三方 entry point

现有 `agent_harness.subagents` entry point 保留，加载规则为：

1. `contract_version == 2`：要求 descriptor 完整，通过后注册；
2. 无 `contract_version` 或值为 1：使用 `LegacyAgentAdapter`；
3. 大于 2、无可调用 factory、名称冲突或 descriptor 不合法：拒绝该 entry point，写 health error；Harness 继续启动，但被拒绝 Agent 不进入 Planner capability catalog；
4. 必需 Agent（planner、verifier、synthesizer）注册失败：Harness 启动失败。

LegacyAgentAdapter 将 AgentTask 转为旧 AgentInput，将旧 AgentResult 转为 AgentReply。Harness 不向 V1 factory 注入原始 ToolRegistry 或 LLMFactory，而是注入 `GovernedToolRegistryProxy` 和 `GovernedLLMFactoryProxy`：前者返回的 tool proxy 只会调用 ToolExecutor，后者返回的 provider proxy 只会调用 LLMExecutor，并继承当前 task 的 scope、预算和 trace。V1 Agent 在受限 worker thread 中运行，旧同步 `complete_text()` 由 GovernedLLMProviderProxy 调用 LLMExecutor 的同步 bridge；bridge 与异步入口共用 UsageLedger、retry 和 circuit breaker。受支持的 V1 合同仅保证注入式依赖受管控；无法接受 proxy 的 factory 或声明/检测到使用 raw dependency 的 Agent 被拒绝注册。系统不声称能可靠发现第三方模块私下捕获的全局 client，管理员必须显式 allowlist 此类 V1 entry point，且默认关闭。Legacy Agent 不允许发送动态消息、handoff 或 GraphPatch，只能执行叶子任务。该适配层不是旧 Orchestrator 执行回退。

### 18.3 Handoff 选择

PolicyGuard 按以下确定性顺序选择 recipient：

1. capability 精确匹配；
2. AgentScope 允许任务所需工具；
3. 排除当前任务祖先链和已达到 handoff 上限的 Agent；
4. `priority` 降序；
5. `name` 字典序。

无候选时 HANDOFF_REQUEST 被拒绝，原任务得到 `NO_CAPABLE_AGENT` AgentError。

## 19. MessageStore 完整 schema 与生命周期

### 19.1 数据库所有权

AgentRuntimeStore 独占 `{data_dir}/agent_runtime.sqlite`，并管理自己的 `schema_migrations`。Web 和 standalone Harness 都必须显式提供 `data_dir`；默认沿用 HarnessConfig.data_dir。Harness 初始化时同步执行轻量 migration，再启动恢复扫描器。

Runtime DB 保存：

- `agent_runs`、`agent_tasks`、`agent_task_dependencies`；
- `agent_task_waits`；
- `agent_messages`、`agent_artifacts`、`runtime_events`；
- `agent_outbox`；
- `agent_operations`、`agent_approvals`；
- `agent_usage_reservations`；
- `agent_legacy_interruptions`。

`agent_legacy_interruptions` 字段为 migration_key 主键、legacy_store_identity、session_id、workflow_name nullable、milestone_id、node_position、original_user_message nullable、state 固定为 LEGACY_INTERRUPTED、replacement_run_id nullable、imported_at。它只保存 legacy 索引和恢复所需最小字段，不复制旧 emitted_events。

SessionStore、Memory L1/L2/L3、EventLog、plan cache 和 web analysis runs 保持现有数据库。跨库更新不宣称原子：Runtime DB 是执行事实源，其他存储通过 outbox 幂等投影。

### 19.2 Outbox

`agent_outbox` 字段：

```text
outbox_id PK
run_id, task_id, message_id NULL
source_event_id NOT NULL
delivery_key NOT NULL
destination                 # scheduler / sse / event_log / audit / session
payload_json
state                       # PENDING / CLAIMED / DELIVERED / DEAD
attempts
available_at
claimed_by, claimed_at
last_error
created_at, delivered_at
UNIQUE(destination, delivery_key)
```

每个 outbox row 都引用一个已持久化 runtime_events.event_id。Agent message 的 delivery_key 为 message_id；纯 task/operation/scheduler event 的 delivery_key 为 event_id；同一 event 发往多个子目标时使用 `event_id|subtarget`。delivery_key 永不为 NULL。

投递算法：

1. 业务事务写 AgentMessage、任务新状态和一个或多个 PENDING outbox row；
2. OutboxWorker 用 compare-and-set 将到期 PENDING row claim 为 CLAIMED；
3. 投递成功写 DELIVERED；失败写回 PENDING，`attempts += 1`，退避为 `min(2**attempts, 60)` 秒；
4. 10 次失败后写 DEAD，并使内部 scheduler destination 对应的 run FAILED；外部 audit/event projection 的 DEAD 不改变已完成 run，但进入 health warning；
5. CLAIMED 超过 60 秒视为 lease 过期，恢复为 PENDING。

稳定幂等键使用 UUIDv5：namespace 为 run_id，name 为 canonical JSON 的 `task_id|execution_attempt|message_type|recipient|causation_id|payload_sha256`。JSON 使用排序 key、UTF-8 和稳定数字序列化。

### 19.3 Runtime event sequence

`agent_runs.next_seq` 从 1 开始。任何 Agent message、task transition、operation transition 或用户可见 SSE 在业务事务内先分配 seq：以 `BEGIN IMMEDIATE` 读取并递增 next_seq，再插入 `runtime_events`。并发 writer 因 SQLite 写锁串行化，唯一约束 `(run_id, seq)` 是第二道保护。

`runtime_events` 字段：

```text
event_id PK
run_id, seq
event_type
task_id NULL
message_id NULL
surface
payload_json              # 已经过 MessageIngestor 脱敏
created_at
UNIQUE(run_id, seq)
```

AgentMessage 与对应 message event 在同一事务使用同一个 seq；纯状态事件只写 runtime_events。SSE、poll 和 resume 只重放 runtime_events，不从 AgentMessage 重新推导历史事件。Tier 1 不创建 Runtime run，由 TurnCoordinator 的单流 `TurnEventSequencer` 分配 turn-local seq，且不支持崩溃重放。

### 19.4 调度恢复

恢复扫描在一个事务中完成：

1. 将 lease 过期 RUNNING task CAS 回 READY；
2. 将 lease 过期 CLAIMED outbox CAS 回 PENDING；
3. 对每个非终态 run 重算 dependency readiness；
4. 依赖已满足的 PLANNED task 转 READY 并写 scheduler outbox；
5. 从 `runtime_events.seq` 恢复 SSE 投影游标，不重新生成已存在的 PROGRESS；
6. 对所有终态任务重算 run 状态，修复崩溃在 task commit 与 run aggregate 之间留下的不一致。

### 19.5 Retention 与删除

- 活跃、等待用户、等待审批和 NEEDS_RECONCILIATION run 不自动清理；
- 正常终态 run、message、artifact 默认保留 30 天；
- Audit 所需 operation 摘要保留 180 天，敏感 args 在 30 天后只保留 hash；
- SessionManager 删除 session 时先取消活跃 run，再删除 Runtime message/artifact；operation audit 按合规保留期脱敏保留；
- 每日 sweeper 删除 orphan artifact、过期 outbox 和已到期 trace。

## 20. 任务、依赖与 Run 状态机

所有状态更新带 `version INTEGER`，使用 `UPDATE ... WHERE version=? AND state IN (...)` 做 compare-and-set。CAS 失败的 worker 必须重新读取，不能覆盖赢家状态。

### 20.1 Task 转换

| From | Event | To |
|---|---|---|
| PLANNED | dependencies satisfied | READY |
| READY | worker claim | RUNNING |
| RUNNING | user-required QUESTION persisted | WAITING_MESSAGE |
| RUNNING | approval required | WAITING_APPROVAL |
| RUNNING | valid RESULT | SUCCEEDED |
| RUNNING | retryable agent failure, execution attempt available | READY |
| RUNNING | accepted agent QUESTION/HANDOFF/REPAIR child request | WAITING_CHILD |
| RUNNING | child request rejected + RESUME_REQUESTER | READY |
| RUNNING | child request rejected + FAIL_REQUESTER | FAILED |
| WAITING_MESSAGE | correlated user ANSWER committed | READY |
| WAITING_CHILD | child RESULT committed | READY |
| WAITING_CHILD | child failed + FAIL_WAITER | FAILED |
| WAITING_CHILD | child failed + RESUME_WITH_FAILURE | READY |
| WAITING_APPROVAL | APPROVED committed | READY |
| WAITING_APPROVAL | REJECTED/EXPIRED | FAILED |
| non-terminal | CANCEL wins CAS | CANCELLED |
| non-terminal | non-retryable error/timeout | FAILED |
| RUNNING | ambiguous external write | INDETERMINATE |
| INDETERMINATE | reconciliation confirms success | SUCCEEDED |
| INDETERMINATE | reconciliation confirms not executed | READY |
| INDETERMINATE | reconciliation confirms failure | FAILED |

`SUCCEEDED`、`FAILED`、`CANCELLED` 是 task 终态。`INDETERMINATE` 是等待 reconciliation 的阻塞状态，不满足任何 dependency。已成功的责任 task 在 repair 中保持 SUCCEEDED；新的 repair child 通过 `repair_of_task_id` 关联。完成与取消竞争时，第一个成功 CAS 的事件生效；后到事件记录为 duplicate/no-op message。

### 20.2 Dependency 语义

每条 edge 带 condition：

- `ON_SUCCESS`：上游必须 SUCCEEDED；
- `ON_TERMINAL`：上游为 SUCCEEDED、FAILED 或 CANCELLED 即可，下游收到成功结果或类型化失败引用；INDETERMINATE 不满足。

required domain task 使用 ON_SUCCESS 连接 evidence verifier；optional domain task 使用 ON_TERMINAL。required task FAILED/CANCELLED 时不释放 verifier并结束 run；任意 task INDETERMINATE 时 run 进入 NEEDS_RECONCILIATION。optional task 明确失败仍释放 verifier，并允许最终 PARTIAL_SUCCESS。

### 20.3 Run 聚合优先级

每次 task 转换后重算：

1. 任一 task INDETERMINATE → run `NEEDS_RECONCILIATION`；
2. 用户取消且无 RUNNING task → CANCELLED；
3. 任一 task WAITING_APPROVAL → WAITING_APPROVAL；
4. 任一 task WAITING_MESSAGE → WAITING_USER；
5. 必需 task 非可修复失败 → FAILED；
6. 任一 task WAITING_CHILD 或其他非终态 task → RUNNING；
7. `AGENT_ANALYSIS` 的 answer verifier 成功且有 optional failure → PARTIAL_SUCCESS；
8. `AGENT_ANALYSIS` 的 answer verifier 成功且无 optional failure → SUCCEEDED；
9. `SYSTEM_COMMAND` 的唯一 SYSTEM_COMMAND task SUCCEEDED → SUCCEEDED；
10. `SYSTEM_COMMAND` task FAILED → FAILED；task CANCELLED → CANCELLED；
11. 其他组合为 FAILED，并写 `INVALID_TERMINAL_COMBINATION`。

`NEEDS_RECONCILIATION` 是非自动恢复的活动状态。reconciliation 先转换 operation/task，再按上述聚合规则把 run 转为 RUNNING、SUCCEEDED、PARTIAL_SUCCESS 或 FAILED；run 不存在 READY 状态。

## 21. Tool operation、HITL 与副作用一致性

### 21.1 Operation 状态

```text
PREPARED
  ├─ WAITING_APPROVAL → APPROVED → EXECUTING
  ├─ REJECTED
  └─ EXECUTING

EXECUTING
  ├─ SUCCEEDED
  ├─ FAILED_SAFE_TO_RETRY
  └─ INDETERMINATE

FAILED_SAFE_TO_RETRY
  ├─ EXECUTING             # retry budget available
  └─ FAILED                # retry budget exhausted

INDETERMINATE
  ├─ SUCCEEDED
  ├─ RETRY_AUTHORIZED → EXECUTING
  └─ FAILED
```

`agent_operations` 包含 operation_id、idempotency_key、task_id、tool_name、args_hash、redacted_args、approval_id、state、remote_idempotency_key、result/error 和 version。`agent_approvals` 包含 approval_id、operation_id、audit_id、decision、decided_by、expires_at 和 version。

### 21.2 工具幂等能力

每个 write tool metadata 必须声明：

- `LOCAL_TRANSACTIONAL`：副作用与 operation outcome 可在同一 SQLite 事务提交；
- `REMOTE_IDEMPOTENT`：远端接受稳定 operation_id；
- `REMOTE_RECONCILABLE`：远端不接受 key，但可以按 operation_id/业务键查询结果；
- `NON_IDEMPOTENT`：既不能去重也不能查询。

`NON_IDEMPOTENT` 工具在 EXECUTING 期间发生进程崩溃或连接结果不明时进入 INDETERMINATE，不自动重试。REMOTE_RECONCILABLE 先运行 reconcile；只有明确未执行时才重试。这样规格不承诺无法实现的 exactly-once 外部副作用。

### 21.3 Confirm 关联

`confirm_request` 必须包含 operation_id、approval_id、run_id、task_id、tool_name、redacted args、impact 和 expires_at。Confirm API 优先按 operation_id 解析；旧客户端未发送 operation_id 时，只在 `(session_id, tool_name, args_hash)` 唯一匹配一个 WAITING_APPROVAL operation 时接受，否则返回 409。

重复确认通过 approval version CAS 返回原决策，绝不重复执行。拒绝、过期或 run 取消均使 task FAILED/CANCELLED 并释放等待协程。`grant_all` 只对当前 session 后续 approval policy 生效，不能自动批准已经进入 INDETERMINATE 的 operation。

### 21.4 Reconciliation

REMOTE_RECONCILABLE operation 进入 INDETERMINATE 后，Runtime 先自动调用 tool metadata 声明的 reconcile handler。无法确定时保持 NEEDS_RECONCILIATION，并由 `POST /api/harness/runs/{run_id}/operations/{operation_id}/reconcile` 接受以下互斥决策：

- `CONFIRMED_SUCCEEDED`：必须附带经过 schema 校验的 result；operation/task 转 SUCCEEDED；
- `CONFIRMED_NOT_EXECUTED`：operation 转 RETRY_AUTHORIZED，task CAS 从 INDETERMINATE 转 READY；ToolExecutor 重新 claim task 时必须先 CAS `RETRY_AUTHORIZED → EXECUTING`，复用原 operation_id；
- `CONFIRMED_FAILED`：operation/task 转 FAILED。

API 要求 operation 当前为 INDETERMINATE、run 为 NEEDS_RECONCILIATION，使用 operation version CAS，并记录 operator 和 reason。成功后 Runtime 重新按第 20.3 节聚合 run：READY task 导致 RUNNING，全部完成则 SUCCEEDED/PARTIAL_SUCCESS，否则 FAILED。重复提交返回第一次 reconciliation 结果，不再次执行工具。

## 22. GraphPatch 协议

```python
class GraphPatch(BaseModel):
    patch_id: str
    run_id: str
    requested_by_task_id: str
    expected_revision: int
    reason_code: Literal[
        "MISSING_EVIDENCE", "VERIFICATION_FAILED",
        "CAPABILITY_MISMATCH", "USER_CONSTRAINT"
    ]
    operations: list[
        AddTaskOp | AddDependencyOp | WaitForTaskOp
    ]
```

```python
class AddTaskOp(BaseModel):
    op: Literal["ADD_TASK"]
    task_key: str
    agent: str
    capability: str
    objective: str
    inputs: dict
    required: bool
    repair_of_task_id: str | None = None
    handoff_from_task_id: str | None = None


class AddDependencyOp(BaseModel):
    op: Literal["ADD_DEPENDENCY"]
    upstream: str  # existing task_id or task_key in this patch
    downstream: str
    condition: Literal["ON_SUCCESS", "ON_TERMINAL"]


class WaitForTaskOp(BaseModel):
    op: Literal["WAIT_FOR_TASK"]
    waiting_task_id: str
    child_task_key: str
    failure_policy: Literal["FAIL_WAITER", "RESUME_WITH_FAILURE"]
```

Runtime 是唯一 apply 方：

1. Agent 只能持久化 HANDOFF_REQUEST 或 REPAIR_REQUEST；Runtime 从该 message 的 payload、message_id 和 requester 当前 graph_revision 确定性构造 GraphPatch。GraphPatch 不是 AgentMessageType，也不经过 AgentMessageDraft；
2. 校验 expected_revision、申请者权限、预算、handoff 深度、repair 次数和无环；
3. 通过时，在单个事务中插入 `GRAPH_PATCH_APPLIED` runtime event、任务/依赖/agent_task_waits、`graph_revision += 1` 和 scheduler outbox，并把 requester 从 RUNNING 改为 WAITING_CHILD；
4. graph revision CAS 冲突时 Runtime 基于同一 request message 重建并重试一次；第二次冲突按 policy rejection 处理，Agent 不重提 raw patch；
5. Policy 拒绝或限制耗尽时写 `GRAPH_PATCH_REJECTED` runtime event，并读取原请求 payload 的 on_reject：`FAIL_REQUESTER` 使 requester FAILED，`RESUME_REQUESTER` 生成 PATCH_REJECTED ANSWER 并使 requester READY；
6. Patch 不能修改其他已经 RUNNING 或终态任务的既有依赖；
7. repair child 不改变原责任 task 的终态；Verifier 恢复后同时读取原 artifact 的拒绝记录和 repair child artifact；
8. handoff child 完成后向 parent 生成 ANSWER，parent 恢复 READY；child 继承 parent 的 required/optional 语义，但 parent 才是原图依赖的 upstream。

限制耗尽时，必需分支 FAILED，可选分支记录失败并继续。不存在无限重提。

## 23. 用户回复、控制消息与上下文所有权

### 23.1 用户回复关联

Chat body 扩展可选字段 `reply_to_message_id`、`run_id` 和 `task_id`，旧字段 `session_id`、`message` 不变。前端收到 `waiting_user` 后必须回传 reply_to_message_id。

兼容旧客户端：

- session 中恰有一个 WAITING_MESSAGE task 时，下一条 message 自动关联；
- 有零个等待任务时，创建新 turn；
- 有多个等待任务且无显式关联时，返回 HTTP 409 `ambiguous_waiting_task`，列出可选 task_id；
- 已回答、已取消或过期 question 的重复回复返回原状态，不创建新 execution attempt。

ANSWER message 与 task `WAITING_MESSAGE → READY` 在同一事务提交。

### 23.2 ContextAssembler 与 TurnRepository

`ContextAssembler` 是 Agent 上下文唯一读取入口，按现有优先级组装 explicit input、当前 PlanGraph dependency artifacts、L1 chat、L2 preferences、L3 references 和系统约束。Agent 不直接读 MemoryManager。

`TurnRepository` 负责：

- session metadata create/touch/token_total；
- turn 与 run 的关联；
- 成功后将 user/final 摘要写入 L1；
- 将焦点 symbol/intent 写入 L2；
- 按现有策略写入 L3 references；
- session 删除时协调 RuntimeStore、Memory、EventLog 和 legacy checkpoint 清理。

Runtime run 失败时不把未验证 draft 写入 L1/L3。

TurnRepository 要求每个可写 Memory backend 实现 `append_projected_exchange(session_id, user_text, assistant_text, projection_key)`。`projection_key = "runtime:" + run_id`，目标 Memory 数据库用唯一索引保存该 key，并在与 L1 两条 message 同一个目标数据库事务中插入 receipt；重复 key 返回 already-applied，不追加消息。当前 SqliteSessionMemory/LangGraph backend 必须在其 session SQLite 中增加 projection receipt 表并共用一个事务；无法提供原子 receipt 的 backend 不能用于生产 Runtime 投影。

### 23.3 现有横切能力 owner

| 当前能力 | 新 owner/接口 |
|---|---|
| lifecycle hooks | `RuntimeLifecycle`; TurnCoordinator 保留 `.lifecycle` facade |
| steer/inject/whenIdle | `RuntimeControlBus`; TurnCoordinator 保留 `.control_bus` facade |
| plan cache | `PlannerAgent` 使用 `PlanCache`; TurnCoordinator `.plan_cache` 只读/配置 facade |
| token usage | `UsageLedger`，仅由 LLMExecutor 预留和结算；ToolExecutor 记录调用次数/延迟而非 token |
| EventBus | RuntimeLifecycle 和 ToolExecutor 发布横切事件 |
| AuditLogger | AuditProjector 消费 outbox |
| session accounting | TurnRepository |
| checkpoint/resume | AgentRuntimeStore message/task/event seq |
| L1/L2/L3 | ContextAssembler + TurnRepository |

### 23.4 跨库会话一致性

Runtime 终态与 Session/L1 投影不能跨 SQLite 文件原子提交。run 进入终态的事务同时分配并写入 `terminal_seq`，并把 `session_projection_state` 置为 PENDING。session projection outbox 的 delivery_key 固定为 `session_projection:<run_id>`。

ContextAssembler 每次组装上下文时先读 Session/L1 及其 projection receipts，再查询同 session 中 `terminal_seq IS NOT NULL AND session_projection_state != DELIVERED` 的 Runtime run。若 target receipt 已有 `runtime:<run_id>`，不添加 overlay；否则把该 run 的已验证 user message、final answer、symbols 和 intent 作为 overlay 合并。overlay 按 run created_at 排序。TurnRepository 调用幂等 append 成功或收到 already-applied 后，把该 run 的 projection state 置 DELIVERED 并写 session_projected_at。进程即使在目标 append 后、Runtime DELIVERED 前崩溃，重试也只命中 receipt，不会重复 L1 message；ContextAssembler 也不会重复 overlay。未验证 draft、失败结果和内部消息不进入 overlay。

## 24. Python、HTTP、Workflow 与 SSE 兼容矩阵

### 24.1 Python API

| 当前表面 | 切换后行为 |
|---|---|
| `Harness.stream_chat` | 保持签名，委托 TurnCoordinator |
| `Harness.resume` | 保持签名，恢复 Runtime run 或执行 legacy restart |
| `set_session_store` | 委托 TurnRepository |
| `set_checkpoint_store` | 仅提供 legacy checkpoint reader，不参与新 run |
| `set_plan_cache_db_path` | 配置 PlannerAgent PlanCache |
| `harness.orchestrator` | 保持，返回精简 TurnCoordinator |
| `.lifecycle/.control_bus/.plan_cache` | 保持 facade |
| `_plan/_execute/_verify/_synthesize/_call_tool` | 私有接口删除；仓库内调用者同提交迁移 |
| `OrchestratorState` 和 CRUD helper | 移到 runtime models/CommandResolver；旧 private import 测试改写 |

WorkflowSpecRegistry 改为注入 `WorkflowServices(tool_executor, agent_runtime, trace_projector)`，不得绑定 Orchestrator 私有方法。`post-execute`、`classify-plan-execute`、`parallel-fetch` 的图形/YAML/list/load 端点保留；它们是模板与可视化接口，不是第二条生产执行路径。

### 24.2 HTTP endpoint

| Endpoint | 兼容规则 |
|---|---|
| `POST /api/harness/chat` | 原 body 有效；新增可选 reply/run/task 关联字段 |
| `POST /api/harness/chat/resume` | 原 session_id 有效；新增可选 run_id、after_seq。未给 run_id 时选择该 session 唯一活动 run；无活动 run 时选择最新可恢复 run；数据异常出现多个活动 run 时返回 409 |
| session list/get/messages/create/patch/delete/fork | 路径与基础响应不变；删除/fork 增加 RuntimeStore 处理 |
| `POST .../confirm` | 原字段有效；新增 operation_id/approval_id，歧义返回 409；endpoint 只提交 approval 决策，禁止像当前实现那样直接 `tool.invoke()` |
| grant_all/revoke_all/get | 路径和语义保持，策略存入 Runtime approval policy |
| `POST /api/harness/chat/batch` | 每个 item 创建独立 run，原聚合响应保持 |
| `GET /api/harness/chat/{session_id}/events` | 改读 runtime_events；新增可选 run_id。无 run_id 时只选择该 session 唯一活动 run，若无活动 run 返回 404；不自动选择任意终态 run。响应始终返回 run_id，后续 poll 应固定传回。`since` 保持零基 offset 并返回 next_since；`after_seq` 走 durable seq；二者互斥 |
| cache/stats | 返回 Planner plan cache + LLM response cache，保留旧 key |
| workflows graph/list/spec/load | 改用 WorkflowServices，不调用 Orchestrator 私有方法 |
| status/health | 增加 RuntimeStore/outbox/dead-letter 状态，保留旧字段 |
| `GET /api/harness/runs/{run_id}/trace` | 新增 Inspector API，cursor 分页 |
| `POST /api/harness/runs/{run_id}/operations/{operation_id}/reconcile` | 新增人工 reconciliation API，仅处理 NEEDS_RECONCILIATION run |

### 24.3 SSE 通用信封

所有持久化业务事件 payload 至少包含：

```text
session_id, turn_id, run_id, seq, surface
```

Tier 1 的 run_id 为 `null`，但仍分配 turn_id 和单调 seq。Tier 2/3 的 seq 在同一 run 内严格递增；Tier 1 的 seq 在同一 `(session_id, turn_id)` 内严格递增。resume/poll 的 `after_seq` 返回 `seq > after_seq`。客户端以 `(run_id or turn_id, seq)` 去重；旧客户端可忽略新增字段。

`resume_complete` 和 `done` 是每条 HTTP SSE 连接的控制事件，不写入 runtime_events，`seq=null`，不参与业务事件去重。Runtime 持久化的是 `RUN_TERMINAL` 状态事件；TraceProjector 在直播或重放结束时由该状态合成 done。

Poll 首次无 run_id 时选择该 session 的唯一活动 run，并在响应中返回 run_id；若因损坏数据出现多个活动 run，返回 409。之后 run_id 明确固定，即使 session 开始新 turn 也不会改变。已知终态 run 仍可凭 run_id 查询。使用 `since` 时，TraceProjector 对固定 run 的可投影事件按 seq 排序后应用 SQL OFFSET，`next_since` 为投影事件总数；使用 `after_seq` 时返回 `next_after_seq`。两种模式不混用。

| 事件 | 必需 payload | 顺序/终止规则 |
|---|---|---|
| `turn/started` | turn_id | 每个 turn 第一条 debug event |
| `plan_started` | intent | Tier 2/3 在 Planner TASK 前 |
| `plan_ready` | steps | 图持久化后、任何领域 task 前 |
| `plan_ready_ptc` | groups | 存在并行 ready group 时紧随 plan_ready，兼容旧 UI |
| `tool_call` | task_id, name, redacted args | 对应 tool_result 前 |
| `tool_result` | task_id, name, ok, result/error | 每次 tool_call 至多一个终结 result |
| `observed` | task counts, errors | 所有领域分支终结后 |
| `verified` | phase, ok, level, details | AGENT_ANALYSIS 的 evidence/answer phase 各一次；SYSTEM_COMMAND 不发 |
| `answer_verified` | ok, level, reason, details | answer phase 后；成功时在 agent_final 前 |
| `confirm_request` | operation/approval/task/tool/impact/expiry | run 随后 WAITING_APPROVAL |
| `audit_decision` | operation_id, approved, status, idempotent | 每个 confirm 请求一次 |
| `agent_final` | tier, result, result_raw, scope | AGENT_ANALYSIS 仅 answer verifier 成功后；SYSTEM_COMMAND 在 command task SUCCEEDED 后发确定性结果 |
| `usage_summary` | totals, by_agent | 每个 run 终结前 |
| `warning`/`error` | code, message, retryable | error 不保证是终态；看 done.status |
| `resume_complete` | run_id, after_seq, last_seq, events_replayed, status | 仅 resume 流，在重放业务事件后、done 前 |
| `done` | run_id, status, terminal_reason | 每个 HTTP SSE 流最后一条；WAITING 状态也关闭当前流；不持久化、不参与后续重放 |
| 新摘要事件 | task_id, agent, summary | 不含完整 prompt 或敏感 args |

并行任务的 tool/progress 事件允许交错，但 plan_ready 必须先于所有领域事件，evidence verified 必须先于 synth，AGENT_ANALYSIS 的 answer_verified 成功必须先于 agent_final，SYSTEM_COMMAND 的 tool_result 必须先于 agent_final，done 必须最后。持久化业务事件断线重放保持原 seq 和 payload，不重新分配序号；旧流中的控制事件不会夹在重放中间。

## 25. Inspector、安全与可量化验收

### 25.1 Inspector contract

`GET /api/harness/runs/{run_id}/trace?cursor=<seq>&limit=<n>` 返回最多 100 条 task/message/artifact metadata，响应含 next_cursor。它使用现有 Harness 单用户认证边界；若部署启用用户身份，必须校验 run 的 session owner。匿名公网访问禁止。

MessageIngestor 在落库前按 Tool schema 的 `sensitive_fields` 和通用 secret key 规则脱敏；TraceProjector 只读取已脱敏记录并生成不同视图。Inspector 不返回原始 secret、Authorization header、API key、完整系统 Prompt 或内部推理。导出和 Audit 使用同一 redaction policy。Session 删除及 retention 按第 19.5 节执行。

### 25.2 精确验收阈值

- `core/orchestrator.py` 与 `turn_coordinator.py` 合计非空非注释行不超过 600；该数字是硬上限，不是估计。
- AST 测试证明 TurnCoordinator 不 import LLM provider、具体 tool 实现或具体 Agent class，不含 `_resolve_action` 类映射。
- 所有六个内置 Agent 至少各有一个经过 AgentDispatcher 的集成测试；Planner、两个 Verifier phase 和 Synthesizer 在每个 Tier 2/3 成功 E2E 中均被调用。
- Runtime fault-injection 在“消息提交前、提交后入队前、tool 返回后 outcome 提交前、task 完成后 run 聚合前”四个边界重启，结果无丢消息；本地和 remote-idempotent 写不重复，非幂等不确定写进入 NEEDS_RECONCILIATION。
- Tier 1 benchmark 使用相同 fake tool 延迟运行 100 次；新旧中位额外开销不超过 5ms，p95 不超过旧路径 1.10 倍，LLM 调用数严格为 0。
- Tier 2/3 使用固定 fake LLM/tool 延迟运行 100 次；Runtime 自身 p95 调度开销不超过 50ms，不把 provider 网络时间计入。
- 第 24.2 节全部 endpoint contract test 通过；第 24.3 节全部 SSE schema、顺序、重放和去重测试通过。
- 每个 task 状态转换都有对应不可变 message 或 runtime event，并可由 run_id 完整重建；这一定义“完整消息”。
- Harness 全量测试通过，当前已知的 Tier 2/PTC 两个失败必须修复或被等价新行为测试替代，不允许 skip/xfail 掩盖。

## 26. Command path 与关键时序

### 26.1 CommandSpec

```python
class CommandSpec(BaseModel):
    command_id: str
    entity: Literal["watchlist", "note", "alert", "scheduled", "run", "report"]
    op: Literal["CREATE", "READ", "UPDATE", "DELETE", "LIST", "RUN", "BULK_DELETE"]
    tool_name: str
    args: dict
    permission: PermissionType
    requires_approval: bool
    result_view: str
```

CommandResolver 是 CommandSpec 的唯一构造者，按 `(entity, op)` 查表并使用类型化 args factory。ToolExecutor 再以 tool schema 校验 args；两层任一失败都不会执行。只读 command 直接执行；写 command 生成全局 task_id 的 SYSTEM_COMMAND task，并使用第 21 节 operation 状态机。

### 26.2 Repair 时序

```text
domain task SUCCEEDED
  → evidence verifier RUNNING
  → REPAIR_REQUEST persisted
  → verifier WAITING_CHILD
  → GraphPatch adds repair child + wait relation
  → repair child SUCCEEDED
  → ANSWER/child RESULT persisted
  → verifier READY → RUNNING
  → verified evidence → synth
```

原 domain task 及其 artifact 保持 immutable；VerifierResult 记录 rejected artifact id。不存在 `SUCCEEDED → REPAIR_PENDING` 转换。

### 26.3 不确定写时序

```text
operation EXECUTING
  → process/network result ambiguous
  → operation/task INDETERMINATE
  → run NEEDS_RECONCILIATION
  → automatic reconcile if supported
  → otherwise operator reconciliation API
  → task SUCCEEDED / READY / FAILED
  → run re-aggregate
```

## 27. UsageLedger 与 LLM 失败恢复

`agent_usage_reservations` 字段：

```text
reservation_id PK
call_id
run_id, task_id, agent_name
execution_attempt, call_ordinal
provider, model
state                    # RESERVED / COMMITTED / EXPIRED_COMMITTED / RELEASED
reserved_input_tokens
reserved_output_tokens
actual_input_tokens
actual_output_tokens
provider_attempt_count
lease_expires_at
created_at, updated_at
UNIQUE(call_id, provider_attempt_count)
```

LLMExecutor 的规则：

1. AgentExecutionContext 为每个 task execution attempt 原子分配递增 `call_ordinal`，生成稳定 UUIDv5 call_id；同一 task 可以进行多次 LLM 调用；
2. 根据实际 prompt token estimate、配置的 max output 和 provider retry 上限计算最坏 reservation；
3. 在 `BEGIN IMMEDIATE` 事务中汇总该 run 的 COMMITTED、EXPIRED_COMMITTED 和 RESERVED token，预算足够才为 `(call_id, provider_attempt_count)` 插入 RESERVED；
4. provider retry 保持 call_id，递增 provider_attempt_count，并使用独立 reservation row；
5. 成功且有 provider usage 时 COMMITTED 实际 usage；明确请求未发送时 RELEASED；
6. 请求已发送后发生 timeout、断连、provider 5xx 且没有可靠 usage 时，立即转 EXPIRED_COMMITTED，并按本次 reservation 上限计入预算。后续 provider retry 使用同一 call_id，但在剩余 run budget 中为下一 provider attempt 新增 child reservation row；预算不足则停止 retry；
7. 进程在 provider 调用期间崩溃时，reservation lease 到期后也转 EXPIRED_COMMITTED，并按 reserved 上限计入预算；恢复任务的新 execution attempt 必须重新预留；
8. provider 响应已经持久化为 task artifact 时，恢复直接复用 artifact，不重新调用；
9. 任何 reservation 后实际 usage 超出估算时仍按真实值 COMMITTED，并阻止 run 的后续 LLM 调用，不能篡改已发生 usage。

Planner、领域 Agent 的 LLM 摘要、Verifier 和 Synthesizer 全部使用 LLMExecutor。ToolExecutor 只处理数据/工具 provider；第 11.1 节的 Tool Provider 和 LLM Provider 错误分别由两个 executor 分类。UsageLedger 汇总直接生成 `usage_summary.by_agent`，不再依赖 Orchestrator 的上下文变量。
