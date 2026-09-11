# 2026-09-12 — Agent Harness Modularization (Harness 独立模块化设计)

**Date:** 2026-09-12
**Stage:** C Day 11e (增量,基于 v2 spec `2026-09-11-finance-general-agent-harness-v2.md`)
**Status:** 🔄 Draft — 待用户 review + 拍板
**Branch:** `codex/finance-general-agent`
**前置依赖:** 无(可基于现有代码开工)

---

## 0. TL;DR

**v2 spec 解决了"harness 怎么改"的问题(D1-D6)。本 spec 解决"harness 该放哪、怎么组织"的问题**(O14=B 5 个 agent + O15=A 3 个 plugin) — 把当前散落在 `tradingagents/agents/general/`(2892 行,9 个文件)的 harness 逻辑抽到独立的 `tradingagents/agent_harness/` 子包,**半独立方案**,新旧并行 3-5 天落地。

**核心答案 3 句话(O14=B, O15=A 拍板)**:
1. **harness 是独立子包** — `tradingagents/agent_harness/`,跟业务 agent(quant/market/news)解耦
2. **5 个真实 sub-agent**(合并 QuoteAgent+FundamentalsAgent 为 DataAgent)— PlannerAgent / VerifierAgent / DataAgent / AlphaAgent / NewsAgent / SynthesizerAgent;其他都是 tool 组合
3. **3 个内置 plugin**(Quant/News/Alert)+ Tool/Plugin Registry 借鉴 OpenBB `entry_points`

---

## 1. Background — Why Modularization

### 1.1 v2 spec 已完成的工作

`2026-09-11-finance-general-agent-harness-v2.md` 的 D1-D6 设计决策:
- D1 三档 Execution Model(Direct Tool / Plan+Execute / Workflow)
- D2 Tool Pydantic 化 + Unified DataResponse
- D3 Workflow 独立(留 Day 15+)
- D4 8 层 Context Priority
- D5 StateGraph 5 节点(plan→execute→observe→verify→synthesize)
- D6 三层 Verification

### 1.2 v2 实施时遇到的 4 个新问题

**问题 1:9 个文件 2892 行,边界模糊**

当前 `tradingagents/agents/general/` 包含:
- `orchestrator.py` (383 行)— 主图 + StateGraph
- `routing.py` (219 行)— intent 分类
- `prompts.py` (297 行)— system prompt 模板
- `tools_bridge.py` (956 行)— 21 个 tool 包装
- `memory.py` (283 行)— L1/L2/L3 记忆
- `audit.py` (201 行)— write audit log
- `approval.py` (87 行)— HITL confirm
- `mcp_server.py` (289 行)— MCP server
- `guardrails.py` (177 行)— guardrails

加 D1-D6 实施后会再膨胀 ~50%。

**问题 2:跟业务 agent 边界不清**

`tradingagents/agents/` 下既有 `general/`(harness)又有未来的 quant/market/news agent。职责混在一起。

**问题 3:扩展性差**

- 加新 tool 必须改 `tools_bridge.py`
- 加新 provider 必须直接 import
- 加新 agent 类型没标准接口
- 没有 plugin 机制

**问题 4:复用性差**

未来若要给其他项目用这个 harness,只能 copy-paste 整个 `general/` 目录,不能作为 library 引用。

### 1.3 v3 设计目标

| # | 目标 | 验证 |
|---|---|---|
| G1 | 抽到 `tradingagents/agent_harness/` 独立子包 | `from tradingagents.agent_harness import Harness` 可用 |
| G2 | 旧 `tradingagents/agents/general/` 路径兼容(3-5 天过渡期) | 现有 9+ 测试套件全不破 |
| G3 | 定义 7 个 sub-agent + 1 个 orchestrator 的标准接口 | `BaseAgent` ABC + 7 个实现 |
| G4 | 实现 Tool Registry + Plugin 机制 | 加新 tool 不改 harness 核心代码 |
| G5 | 高可用 10 项全部落地 | Health check / circuit breaker / failover 全工作 |
| G6 | 高效 10 项全部落地 | Tier 1 短路径 + 并行 invoke + cache 全工作 |

---

## 2. 拍板的 2 个关键决策(Q1/Q2,2026-09-12)

### Q1 — 模块独立度

| 选项 | 含义 | 工期 |
|---|---|---|
| A 完全独立 | 新建顶级 `agent_harness/` 包,跟 `tradingagents/` 平级,迁全部代码 | 7-10 天 |
| **B 半独立** ✅ | 新建 `tradingagents/agent_harness/` 子包,跟业务 agent 平级,**新旧并行**(旧代码保留,逐步迁) | **3-5 天** |
| C 暂不抽 | 继续在 `tradingagents/agents/general/` 内改 | 2-3 天 |

**为什么选 B**:
- ✅ **风险可控** — 旧代码保留,新代码可独立测试
- ✅ **渐进式** — 每天迁 1-2 个模块,出问题可回滚
- ✅ **面向未来** — 未来若需要可平滑升到 A(顶级独立包)
- ❌ 不选 A 的理由:7-10 天太重,且跟当前 v2 实施节奏冲突
- ❌ 不选 C 的理由:治标不治本,继续恶化扩展性问题

### Q2 — 文档组织

| 选项 | 含义 |
|---|---|
| A 写进 v2 spec | 把本设计作为 v2 spec 的 v3 增量章节 |
| **B 新建独立 spec** ✅ | 单独成 `2026-09-12-agent-harness-modularization.md`(本文),跟 v2 spec **平级 + 互引** |

**为什么选 B**:
- ✅ v2 spec(D1-D6)和本 spec(Q1+Q2)是**正交维度** — D 回答"怎么改",Q 回答"放哪 + 怎么组织"
- ✅ 各 spec 独立可读,review 负担小
- ✅ 未来真要做 A(顶级独立包),可直接基于本 spec 升级

---

## 3. 目录结构(半独立方案)

```
tradingagents/
├── agent_harness/                       [NEW] 独立 harness 子包
│   ├── __init__.py                      公共 API export
│   ├── harness.py                       Harness 主类(组装所有组件)
│   │
│   ├── core/                            编排核心(原 orchestrator.py / routing.py 拆开)
│   │   ├── __init__.py
│   │   ├── orchestrator.py              Tier 2 StateGraph 5 节点 (D5)
│   │   ├── tier.py                      Tier 路由 (D1) + classify_tier()
│   │   ├── context.py                   ContextPriority 8 层注入 (D4)
│   │   ├── verification.py              三层 verification (D6)
│   │   ├── retry.py                     retry + backoff + circuit breaker
│   │   └── short_circuit.py             Tier 1 server-side 强执行 (D1)
│   │
│   ├── llm/                             LLM 适配层(从 web/app.py 拆)
│   │   ├── __init__.py
│   │   ├── base.py                      LLMProvider ABC
│   │   ├── registry.py                  LLM_REGISTRY dict
│   │   ├── openai_provider.py           兼容 OpenAI 协议(Claude/MiniMax/Ollama 都走这)
│   │   └── factory.py                   create_llm(provider_name, model_name) → LLM
│   │
│   ├── data/                            数据层(D2)
│   │   ├── __init__.py
│   │   ├── providers/
│   │   │   ├── __init__.py
│   │   │   ├── base.py                  Provider ABC
│   │   │   ├── yfinance_provider.py
│   │   │   ├── eastmoney_provider.py
│   │   │   ├── akshare_provider.py
│   │   │   └── registry.py              PROVIDERS dict + get_active_provider()
│   │   ├── responses.py                 DataResponse / QuoteData / HistoryData
│   │   ├── cache.py                     provider result cache(SQLite)
│   │   └── failover.py                  provider failover 策略
│   │
│   ├── tools/                           Tool 框架(M3 fix 2026-09-11 扁平化)
│   │   ├── __init__.py
│   │   ├── base.py                      BaseTool ABC
│   │   ├── registry.py                  ToolRegistry
│   │   ├── schema.py                    Pydantic v2 schema 强校验 + JSON parser
│   │   ├── permission.py                HITL permission policy(scope-based)
│   │   │                                ↓ 扁平化(原本 builtin/write/ 嵌套改扁平)
│   │   ├── builtin_quote.py             get_quote + get_quotes_batch
│   │   ├── builtin_history.py           get_history
│   │   ├── builtin_fundamentals.py      get_fundamentals
│   │   ├── builtin_news.py              get_news
│   │   ├── builtin_alpha.py             compute_alpha_factors + evaluate_alpha
│   │   ├── write_alert.py               create/update/delete_alert(HITL)
│   │   ├── write_note.py                create/update/delete_note(HITL)
│   │   └── write_scheduled.py           create/update/delete_scheduled_task(HITL)
│   │
│   ├── memory/                          分层记忆(原 memory.py 拆)
│   │   ├── __init__.py
│   │   ├── base.py                      MemoryLayer ABC
│   │   ├── l1_session.py                SqliteSaver(session 隔离)
│   │   ├── l2_preferences.py            user_preferences
│   │   └── l3_references.py             agent_references
│   │
│   ├── agents/                          Sub-agent 定义(Tier 3) — O14=B 拍板 5 个
│   │   ├── __init__.py
│   │   ├── base.py                      BaseAgent ABC + AgentInput/AgentResult
│   │   ├── registry.py                  AgentRegistry
│   │   ├── planner.py                   PlannerAgent (Tier 2/3 plan 生成)
│   │   ├── synthesizer.py               SynthesizerAgent (LLM 合成回答)
│   │   ├── verifier.py                  VerifierAgent (L3 LLM-judge)
│   │   ├── data_agent.py                DataAgent ⭐ 合并 QuoteAgent + FundamentalsAgent
│   │   ├── alpha_agent.py               AlphaAgent
│   │   └── news_agent.py                NewsAgent
│   │
│   ├── workflow/                        Tier 3 DAG 编排(D3,Day 15+ 实施)
│   │   ├── __init__.py
│   │   ├── runner.py                    WorkflowRunner
│   │   ├── dag.py                       DAG 定义 + topological sort
│   │   └── progress.py                  SSE progress 推送
│   │
│   ├── plugins/                         Plugin 系统 — O15=A 拍板 3 个内置 plugin
│   │   ├── __init__.py
│   │   ├── base.py                      Plugin ABC
│   │   ├── registry.py                  PluginRegistry + entry_points 发现
│   │   └── builtin/                     内置 3 个 plugin
│   │       ├── __init__.py
│   │       ├── quant.py                 [Plugin 1] Alpha158 + AlphaAgent + compute_alpha_factors
│   │       ├── news.py                  [Plugin 2] NewsAgent + get_news + sentiment
│   │       └── alert.py                 [Plugin 3] AlertTool (write) + NoteTool (write)
│   │
│   ├── observability/                   可观测性(高可用关键)
│   │   ├── __init__.py
│   │   ├── audit.py                     write audit log(原 audit.py 迁)
│   │   ├── metrics.py                   token/latency/error rate 收集
│   │   ├── health.py                    /api/harness/health endpoint
│   │   └── tracing.py                   链路追踪(OpenTelemetry-lite)
│   │
│   ├── config/                          配置驱动
│   │   ├── __init__.py
│   │   ├── schema.py                    HarnessConfig Pydantic model
│   │   └── loader.py                    from_yaml / from_env
│   │
│   └── mcp/                             MCP server(Day 5 已做,迁移)
│       ├── __init__.py
│       └── server.py                    FastMCP,自动从 ToolRegistry 暴露
│
├── agents/                              [保留] 业务 agent(quant / market / news 等)
│   ├── general/                         [过渡保留 3-5 天] 原 general/ 代码
│   │   └── (DEPRECATED.py → 内部 re-export 到 agent_harness)
│   ├── quant/
│   ├── market/
│   └── news/
│
├── dataflows/                           [保留] 底层数据访问(被 data/providers/ 包装)
└── (其他)

web/
├── app.py                               [MOD] 改 import 路径从 agent_harness
├── routes/
│   ├── agent.py                         [MOD] 用 harness.stream_chat()
│   └── harness_health.py                [NEW] /api/harness/health
└── static/                              [不变]

tests/
├── test_harness_*.py                    [NEW] agent_harness 全套测试
├── test_d11*.py                         [保留] 旧测试,逐步改 import
└── test_d12*.py ...                     [NEW] 新功能测试
```

---

## 4. 子 Agent 划分(5 个真实 sub-agent)

**关键原则**:LLM agent 是贵资源,大部分场景是 tool 组合,只有真正需要 LLM 推理的才用 agent。

### 4.1 5 个 sub-agent 详细定义

| Sub-agent | 角色 | 工具集 | LLM 调用 | 何时启用 |
|---|---|---|---|---|
| **PlannerAgent** | JSON plan 生成 | (无 tool,纯推理) | 1 次 | Tier 2/3 入口 |
| **VerifierAgent** | L3 LLM-judge | (无 tool) | 0-1 次 | Tier 2/3 验证 |
| **DataAgent** ⭐ 合并 | 行情 + 基本面(**不含** get_history) | `get_quote / get_quotes_batch / get_fundamentals` | 0 次(可纯 server-side) | Tier 3 数据子任务 / Tier 1 短路径 |

**DataAgent 边界澄清**(C1 fix,2026-09-11):
- DataAgent **不包含** `get_history` — `get_history` 是 AlphaAgent 的前置工具
- Tier 1 短路径**不**经过 DataAgent(直接 `PROVIDERS.get_quote(args)`)
- 如果未来 get_history 在 Tier 1 短路径也用得着,再单独抽 `HistoryAgent`(不在本 spec 范围)|
| **AlphaAgent** | 量化因子 | `compute_alpha_factors / evaluate_alpha` | 1 次(解释因子) | Tier 3 量化子任务 |
| **NewsAgent** | 新闻舆情 | `get_news` | 1 次(sentiment 总结) | Tier 3 新闻子任务 |
| **SynthesizerAgent** | 回答合成 | (基于已 verified 数据) | 1 次 | Tier 2/3 结尾 |

**DataAgent 合并理由**(O14=B 拍板):
- 行情和基本面查询通常是连续动作("查价 → 看基本面")
- 合并后一个 agent 内部可共享 LLM context(查完价后基本面的 query 可用同一 context)
- Tier 3 DAG 节点数从 4 个降到 3 个(DAG 更简洁)
- 新增 Tier 1 短路径(DataAgent 可零 LLM 完成 80% query)

**说明**:
- QuoteAgent / FundamentalsAgent / AlphaAgent / NewsAgent 是 Tier 3 DAG 的并行节点
- PlannerAgent / VerifierAgent / SynthesizerAgent 是 Tier 2/3 的控制流节点
- **不是每个业务域都要 agent** — 例如 schedule / preference update 直接走 tool,不开 agent

### 4.2 BaseAgent 标准接口

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel, Field
from typing import AsyncIterator, Literal

class AgentInput(BaseModel):
    """所有 agent 输入统一格式。"""
    user_message: str
    context: dict = Field(default_factory=dict)
    plan_step: dict | None = None  # 来自 PlannerAgent


class AgentResult(BaseModel):
    """所有 agent 输出统一格式。"""
    success: bool
    content: str = ""
    structured_data: dict | None = None
    tool_calls: list[dict] = Field(default_factory=list)
    tool_results: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class BaseAgent(ABC):
    """所有 sub-agent 必须实现这个接口。

    关键约束:
    - name / description 给 PlannerAgent 用
    - tools 是固定子集(不是动态注册)
    - run() 返回 AgentResult(不是 str)
    - 支持 sync / async / streaming
    """

    name: str                                       # "quote_agent"
    description: str                                # "查询标的最新行情,返回价格/涨跌幅/成交量"
    tools: list                                     # 固定工具集
    system_prompt: str                              # 默认 prompt,可被 plugin 覆盖
    timeout_seconds: float = 60.0

    @abstractmethod
    async def run(
        self,
        input: AgentInput,
        *,
        context: AgentContext,
    ) -> AgentResult:
        """同步运行。"""
        ...

    async def stream(
        self,
        input: AgentInput,
        *,
        context: AgentContext,
    ) -> AsyncIterator[tuple[str, dict]]:
        """流式运行(M2 fix,2026-09-11:默认实现,子类可覆盖)。

        默认实现:调 run() 后 yield final 事件。
        子类(特别是 Tier 3 多 agent)可覆盖 emit 中间 plan / tool_call / tool_result 事件。
        """
        result = await self.run(input, context=context)
        yield ("agent_final", result.model_dump())

    def get_plan_steps(self) -> list[dict]:
        """告诉 PlannerAgent 自己能完成哪些 step。"""
        return [{"agent": self.name, "capability": self.description}]
```

### 4.3 AgentRegistry(agent 注册表)

```python
class AgentRegistry:
    """所有 sub-agent 通过这个注册表管理。

    加新 agent = 注册一行,不改 harness 核心代码。
    """

    def __init__(self):
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        if agent.name in self._agents:
            raise ValueError(f"agent {agent.name} already registered")
        self._agents[agent.name] = agent
        LOGGER.info("agent registered: %s", agent.name)

    def get(self, name: str) -> BaseAgent:
        if name not in self._agents:
            raise KeyError(f"agent {name} not registered")
        return self._agents[name]

    def list(self) -> list[str]:
        return list(self._agents.keys())

    def plan_capabilities(self) -> list[dict]:
        """汇总所有 agent 的能力,给 PlannerAgent 用。"""
        caps = []
        for agent in self._agents.values():
            caps.extend(agent.get_plan_steps())
        return caps


# 使用示例
registry = AgentRegistry()
registry.register(QuoteAgent())
registry.register(FundamentalsAgent())
# ...
plan = await PlannerAgent().plan(
    user_message="分析 600036 估值合理性",
    capabilities=registry.plan_capabilities(),
)
```

---

## 5. Tool / Plugin 系统(高扩展关键)

### 5.0 Pydantic 版本(M4 fix,2026-09-11)

**明确使用 Pydantic v2**:`from pydantic import BaseModel, Field, field_validator`
- v1 项目请先升级,新写 schema 必须用 v2 API
- v2 vs v1 关键差异:`validator` → `field_validator`,`root_validator` → `model_validator`,`Config` class → `model_config`
- Field 元数据:`Field(default, description="...", pattern=...)`(v2 用 `pattern` 替代 v1 的 `regex`)

### 5.1 3 层 Tool 体系

```
┌─────────────────────────────────────────────────────────┐
│ Layer 3: Workflow Tools(跨多 tool 组合)                  │
│   - run_trading_agents_analysis                         │
│   - run_scheduled_task                                  │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│ Layer 2: Write Tools(HITL 需 approval)                  │
│   - create_alert / update_alert / delete_alert          │
│   - create_note / update_note / delete_note             │
│   - create_scheduled_task / ...                         │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│ Layer 1: Read Tools(server-side 友好,无 LLM 也能调)     │
│   - get_quote / get_quotes_batch                        │
│   - get_history / get_fundamentals / get_news           │
│   - compute_alpha_factors / evaluate_alpha              │
│   - list_watchlist / list_scheduled_tasks               │
└─────────────────────────────────────────────────────────┘
```

### 5.2 Tool 标准 Schema

```python
class PermissionType(str, Enum):
    READ = "read"
    WRITE = "write"           # HITL required
    WORKFLOW = "workflow"     # 长任务,异步


class ToolSchema(BaseModel):
    """Tool 契约标准。"""
    name: str
    description: str
    args_schema: type[BaseModel]
    result_schema: type[BaseModel]
    permission: PermissionType
    timeout_seconds: float = 30.0
    cache_ttl_seconds: int = 60
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class BaseTool(ABC):
    """所有 tool 必须实现这个接口。"""

    schema: ToolSchema

    @property
    def name(self) -> str:
        return self.schema.name

    @abstractmethod
    async def invoke(self, args: BaseModel, context: ToolContext) -> BaseModel:
        """主入口。返回 result_schema 实例。"""
        ...

    def to_langchain(self) -> StructuredTool:
        """转 LangChain StructuredTool(给 Tier 2 LLM 调用)。"""
        ...


@dataclass
class ToolContext:
    """tool 调用上下文。"""
    session_id: str
    user_id: str | None = None
    intent: Intent | None = None
    tier: int | None = None
    trace_id: str | None = None
```

### 5.3 ToolRegistry(借鉴 OpenBB entry_points)

```python
class ToolRegistry:
    """Tool 注册表。

    三种注册方式:
    1. decorator(代码内)
    2. 手動 .register()(代码内)
    3. entry_points(第三方 plugin,运行时发现)
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._permission_index: dict[PermissionType, set[str]] = {
            p: set() for p in PermissionType
        }

    def register(
        self,
        *,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        result_schema: type[BaseModel],
        permission: PermissionType = PermissionType.READ,
        timeout_seconds: float = 30.0,
        cache_ttl_seconds: int = 60,
    ):
        """Decorator 形式。"""
        def decorator(func):
            schema = ToolSchema(
                name=name,
                description=description,
                args_schema=args_schema,
                result_schema=result_schema,
                permission=permission,
                timeout_seconds=timeout_seconds,
                cache_ttl_seconds=cache_ttl_seconds,
            )
            tool = FunctionTool(func, schema)
            self._add_tool(tool)
            return func
        return decorator

    def add(self, tool: BaseTool) -> None:
        """手動注册(给 plugin 用)。"""
        self._add_tool(tool)

    def _add_tool(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name} already registered")
        self._tools[tool.name] = tool
        self._permission_index[tool.schema.permission].add(tool.name)
        LOGGER.info("tool registered: %s (permission=%s)", tool.name, tool.schema.permission)

    def get(self, name: str) -> BaseTool:
        return self._tools[name]

    def list_by_permission(self, perm: PermissionType) -> list[BaseTool]:
        return [self._tools[n] for n in self._permission_index[perm]]

    def discover_entry_points(self, group: str = "agent_harness.tools") -> None:
        """从 entry_points 自动发现第三方 tool。"""
        try:
            import importlib.metadata as md
            eps = md.entry_points(group=group)
            for ep in eps:
                tool = ep.load()()  # 调用 factory
                self.add(tool)
                LOGGER.info("discovered tool via entry_points: %s", tool.name)
        except Exception as e:
            LOGGER.warning("entry_points discovery failed: %s", e)


# 使用示例
tool_registry = ToolRegistry()

@tool_registry.register(
    name="get_quote",
    description="获取单个标的最新行情",
    args_schema=QuoteArgs,
    result_schema=QuoteResult,
    permission=PermissionType.READ,
    cache_ttl_seconds=60,
)
async def get_quote(args: QuoteArgs) -> QuoteResult:
    provider = get_active_provider()
    raw = await provider.get_quote(args.symbol, args.asset_type)
    return QuoteResult(
        results=raw,
        provider=provider.name,
        fetched_at=datetime.utcnow(),
        warnings=[],
    )
```

### 5.4 Plugin 系统(借鉴 OpenBB entry_points)

```python
class Plugin(ABC):
    """Plugin 契约。

    第三方 plugin 通过 pyproject.toml 注册:
        [project.entry-points."agent_harness.plugins"]
        my_plugin = "my_pkg.plugin:MyPlugin"
    """

    name: str
    version: str
    description: str = ""

    @abstractmethod
    def tools(self) -> list[BaseTool]:
        """返回这个 plugin 提供的 tool。"""
        ...

    def agents(self) -> list[BaseAgent]:
        """返回这个 plugin 提供的 agent(可选)。"""
        return []

    def prompts(self) -> dict[str, str]:
        """覆盖默认 prompt(name → content)。"""
        return {}

    def config_schema(self) -> type[BaseModel] | None:
        """plugin 配置 schema(可选)。"""
        return None


class PluginRegistry:
    """Plugin 注册表 + entry_points 自动发现。"""

    def __init__(self, harness: "Harness"):
        self.harness = harness
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        """手动注册(给代码内 plugin 用)。"""
        if plugin.name in self._plugins:
            raise ValueError(f"plugin {plugin.name} already registered")
        self._plugins[plugin.name] = plugin
        plugin.install(self.harness)
        LOGGER.info("plugin installed: %s v%s", plugin.name, plugin.version)

    def discover_entry_points(self, group: str = "agent_harness.plugins") -> None:
        """从 entry_points 自动发现第三方 plugin。"""
        try:
            import importlib.metadata as md
            for ep in md.entry_points(group=group):
                plugin_cls = ep.load()
                plugin = plugin_cls()
                self.register(plugin)
                LOGGER.info("discovered plugin via entry_points: %s", plugin.name)
        except Exception as e:
            LOGGER.warning("plugin entry_points discovery failed: %s", e)

    def install_all(self) -> None:
        """安装所有 plugin(tool/agent/prompt 全部注册到 harness)。"""
        for plugin in self._plugins.values():
            plugin.install(self.harness)


# 使用示例
class QuantPlugin(Plugin):
    name = "quant"
    version = "1.0.0"
    description = "Alpha158 量化因子计算"

    def tools(self):
        return [ComputeAlphaFactorsTool(), EvaluateAlphaTool()]

    def agents(self):
        return [AlphaAgent()]

    def prompts(self):
        return {
            "alpha_explanation": "你是量化研究员,擅长解释 alpha 因子的 IC/IR ...",
        }
```

### 5.5 完整目录 vs plugin 化迁移路径

**Phase 1**:`tools/builtin/` 直接放 tool 代码(类似 FastAPI 的 router pattern)
**Phase 2**:每个 `builtin/` 子目录变成 `plugins/builtin/` 子模块,用 `Plugin` 类包装
**Phase 3**:第三方 plugin 通过 `pip install tradingagents-plugin-xxx` 安装,自动被发现

---

## 6. Harness 主类(组装所有组件)

```python
class Harness:
    """Harness 主类 — 组装所有组件,提供统一入口。

    **Init order**(C2 fix,2026-09-11,严格按此顺序):
    1. config (HarnessConfig.from_env)
    2. tool_registry (ToolRegistry)
    3. agent_registry (AgentRegistry)
    4. llm_factory (LLMFactory)
    5. data_registry (PROVIDERS dict)
    6. memory (MemoryManager)
    7. audit (AuditLogger)
    8. health (HealthChecker)
    9. context_priority (ContextPriority)
    10. retry_policy + circuit_breaker
    11. tier_router (TierRouter)
    12. orchestrator (Orchestrator,依赖 2-11)
    13. plugin_registry (PluginRegistry,最后注册所有 plugin)
    """

    def __init__(self, config: HarnessConfig | None = None):
        self.config = config or HarnessConfig.from_env()

        # Init order 见 class docstring
        # 初始化各组件
        self.tool_registry = ToolRegistry()
        self.agent_registry = AgentRegistry()
        self.llm_factory = LLMFactory()
        self.data_registry = PROVIDERS
        self.memory = MemoryManager(self.config.data_dir)
        self.audit = AuditLogger(self.config.data_dir)
        self.health = HealthChecker()

        # 初始化各层
        self.context_priority = ContextPriority(self.config.context)
        self.retry_policy = RetryPolicy.from_config(self.config.retry)
        self.circuit_breaker = CircuitBreaker(self.config.circuit_breaker)
        self.tier_router = TierRouter(self.config.tier)

        # orchestrator(Tier 2 主图)
        self.orchestrator = Orchestrator(
            tool_registry=self.tool_registry,
            agent_registry=self.agent_registry,
            llm_factory=self.llm_factory,
            context_priority=self.context_priority,
            retry_policy=self.retry_policy,
            circuit_breaker=self.circuit_breaker,
            audit=self.audit,
        )

        # plugin 注册
        self.plugin_registry = PluginRegistry(self)
        self.plugin_registry.discover_entry_points()
        self._install_builtin_plugins()

    def _install_builtin_plugins(self) -> None:
        """安装内置 plugin。"""
        from .plugins.builtin import QuantPlugin, NewsPlugin, AlertPlugin
        self.plugin_registry.register(QuantPlugin())
        self.plugin_registry.register(NewsPlugin())
        self.plugin_registry.register(AlertPlugin())

    async def stream_chat(
        self,
        session_id: str,
        user_message: str,
        *,
        history: list | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """统一入口 — 由 web/routes/agent.py 调用。"""
        async for event in self.orchestrator.stream_chat(
            session_id=session_id,
            user_message=user_message,
            history=history,
        ):
            yield event

    async def health_check(self) -> dict:
        """健康检查 — 返回每个组件状态。"""
        return await self.health.check_all(self)

    def get_tool(self, name: str) -> BaseTool:
        return self.tool_registry.get(name)

    def get_agent(self, name: str) -> BaseAgent:
        return self.agent_registry.get(name)
```

---

### 6.1 Entry Points 本地测试方法(C4 fix,2026-09-11)

```python
# tests/fixtures/test_plugin/__init__.py — 测试用 plugin
from tradingagents.agent_harness.plugins.base import Plugin
from tradingagents.agent_harness.tools.base import BaseTool, ToolSchema, PermissionType
from pydantic import BaseModel

class TestArgs(BaseModel):
    msg: str

class TestResult(BaseModel):
    echo: str

class TestTool(BaseTool):
    schema = ToolSchema(
        name="test_echo",
        description="测试用 echo tool",
        args_schema=TestArgs,
        result_schema=TestResult,
        permission=PermissionType.READ,
    )
    async def invoke(self, args, context):
        return TestResult(echo=args.msg)

class TestPlugin(Plugin):
    name = "test_plugin"
    version = "0.1.0"
    def tools(self):
        return [TestTool()]

# 验证
from tradingagents.agent_harness import Harness
from tradingagents.agent_harness.plugins.registry import PluginRegistry
h = Harness()
reg = PluginRegistry(h)
reg.discover_entry_points(group="agent_harness.plugins")
assert "test_echo" in h.list_tools()
```

**pyproject.toml 注册**:
```toml
[project.entry-points."agent_harness.plugins"]
my_plugin = "my_pkg.plugin:MyPlugin"
test_plugin = "tests.fixtures.test_plugin:TestPlugin"
```

---

## 7. 高扩展性 + 高可用 + 高效(精简版)

### 7.1 高扩展性(8 项)— D4 由 v2 spec P4 实施

**注意**:D4 8 层 Context Priority 由 **v2 spec P4** 负责实施(2026-09-13),本 spec v3 不实施。本 spec §7.1 仅列 D4 设计描述,实施时引用 v2 spec。

### 7.1 高扩展性(8 项)

| # | 设计 | 实现位置 |
|---|---|---|
| 1 | Plugin 注册机制 | `plugins/registry.py` + `entry_points` 自动发现 |
| 2 | Tool adapter pattern | `tools/base.py` 的 `BaseTool` ABC |
| 3 | Provider 抽象 | `data/providers/base.py` 的 `Provider` ABC |
| 4 | Agent factory 模式 | `agents/registry.py` 的 `AgentRegistry` |
| 5 | Config 驱动 | `config/schema.py` 的 `HarnessConfig`(YAML + env) |
| 6 | Hook 系统 | `core/hooks.py`(pre/post tool_call / agent_run) |
| 7 | Prompt 覆盖 | `plugins/builtin/*` 的 `prompts()` 方法 |
| 8 | Permission policy | `tools/permission.py` 声明式 scope-based |

### 7.2 高可用(10 项)

| # | 设计 | 实现 |
|---|---|---|
| 1 | 降级策略 | LLM 不可用 → 自动降级到 Tier 1 short circuit |
| 2 | Provider failover | 主 provider 失败 → 自动切备用 |
| 3 | Retry + backoff | 指数退避,max 3 次,区分 transient/permanent |
| 4 | Timeout 边界 | 每个 tool / LLM / request 独立 timeout |
| 5 | Rate limit aware | `data/providers/base.py` 的 rate limiter |
| 6 | Circuit breaker | `core/retry.py` 的 `CircuitBreaker` 类 |
| 7 | State 持久化 | `memory/l1_session.py` SqliteSaver |
| 8 | Health check | `observability/health.py` + `/api/harness/health` |
| 9 | Graceful degradation | tool 返回 `DataResponse(warnings=[err])` 不抛异常 |
| 10 | Audit + 告警 | `observability/audit.py` + 飞书 webhook |

### 7.3 高效(10 项)

| # | 设计 | 实现 |
|---|---|---|
| 1 | Tier 1 short circuit | `core/short_circuit.py`(零 LLM) |
| 2 | 并行 tool invoke | `asyncio.gather` in `orchestrator.py` |
| 3 | LLM result cache | `llm/cache.py`(key=prompt+model+temp, TTL 5min) |
| 4 | Tool result cache | `data/cache.py`(key=provider+endpoint+params) |
| 5 | Streaming SSE | `web/routes/agent.py` 已有,只需改 import |
| 6 | Plan 复用 | `core/plan_template.py`(类似 query 用同一 plan) |
| 7 | Lazy plugin load | `plugins/registry.py` support `load_on_demand=True` |
| 8 | Connection pooling | `data/providers/base.py` aiohttp.ClientSession 复用 |
| 9 | Pre-fetch | `core/prefetch.py`(预测下一步) |
| 10 | Tier 3 DAG 并行 node | `workflow/runner.py` topological sort + parallel |

---

## 8. 与 v2 spec / v1 spec 的关系

| 来源 | 关系 |
|---|---|
| v1 spec `2026-09-10-finance-general-agent-design.md` | 上游 — 已拍板 O5-O10,实现已基本完成 |
| v2 spec `2026-09-11-finance-general-agent-harness-v2.md` | 平级互引 — D1-D6 是"怎么改",本 spec 是"放哪 + 怎么组织" |
| 后续 v4 spec (待定) | 可能是 Workflow 独立(D3 留 Day 15+)或多用户隔离 |

### 本 spec 增量内容(相对 v2)

| v2 spec 内容 | 本 spec 增量 |
|---|---|
| D1-D6 设计决策 | ✅ 不变 |
| 实施 Roadmap P0-P4 | ✅ 替换为更详细的 P1-P7(见 §9) |
| File Manifest(v2 写了 14 个文件) | ✅ 扩展到 50+ 文件(本 spec §3) |
| 7 个 sub-agent 表(本 spec §4) | ✅ 新增 — v2 没具体化 sub-agent |
| Tool / Plugin 系统(本 spec §5) | ✅ 新增 — v2 只说 Pydantic 化 |
| Harness 主类(本 spec §6) | ✅ 新增 — v2 没组装入口 |
| 高扩展 / 高可用 / 高效 | ✅ 不变(v2 写过,本 spec §7 精简版) |

---

## 9. 实施 Roadmap(7 个 Phase,3-5 天)— 与 v2 spec 编号对齐说明

**重要**:v3 spec 的 P1-P7 与 v2 spec 的 P0-P4 是**独立编号**,不是同一时间轴。
- v2 spec P0 = 设计拍板 / P1 = Tier 1 short circuit / P2 = StateGraph 5 节点 / P3 = verification / P4 = Context Priority
- v3 spec P1 = 创建骨架(已完成 2026-09-11)/ P2 = Provider ABC + DataResponse / P3 = Tool Pydantic 化 / P4 = Orchestrator 实现 / P5 = 5 个 sub-agent / P6 = Plugin 系统 / P7 = Observability
- **依赖关系**:v3 P1 必须先于 v2 P1(因为 v2 P1 实施时已经引用 v3 的 ToolRegistry)
- **总时间** ≈ v2 P0-P4 (3-4 天) + v3 P2-P7 (3-4 天) + 30% 迁移成本 = **总计 ~7-10 天**(不是 v3 spec 原写的 3-4 天)

---

| Phase | 时间 | 内容 | 验证 |
|---|---|---|---|
| **P1** | 0.5 天 | 创建 `agent_harness/` 目录骨架 + `__init__.py` + `Harness` 主类 + 旧 `general/` 代码 re-export | `from tradingagents.agent_harness import Harness` 可用,旧测试不破 |
| **P2** | 0.5 天 | `data/providers/base.py` + `yfinance/eastmoney/akshare` 3 个 provider + `PROVIDERS` registry + `DataResponse` | 切换 provider 测试 + 旧 quote/fundamentals 测试不破 |

### 9.1 P1-P7 ↔ §7 高扩展/可用/高效 28 项映射(M1 fix,2026-09-11)

| Phase | 实施 §7.1 高扩展(8 项) | 实施 §7.2 高可用(10 项) | 实施 §7.3 高效(10 项) |
|---|---|---|---|
| **P1** ✅ | (无) | (无) | (无,纯骨架) |
| **P2** | #2 Tool adapter | (无) | #8 Connection pooling |
| **P3** | #1 Plugin 注册 / #5 Config / #6 Hook(预留) | #9 Graceful degradation | #1 Tier 1 短路径 / #5 Streaming |
| **P4** | #3 Provider 抽象 / #4 Agent factory / #7 Prompt 覆盖 / #8 Permission | #4 Timeout / #7 State 持久化 | #2 并行 invoke / #6 Plan 复用 |
| **P5** | (P5 是 agents,不算高扩展) | (无) | (无) |
| **P6** | (核心就是 plugin 完整化) | (无) | #7 Lazy plugin load |
| **P7** | (无) | #1 降级 / #2 Failover / #3 Retry / #5 Rate limit / #6 Circuit breaker / #8 Health check / #10 Audit | #10 Tier 3 DAG 并行 |

**未覆盖**(留 Day 15+):#4 Pre-fetch / #9 Pre-fetch,这两个留 Phase 3 后期或后续 spec 实施。
| **P3** | 0.5 天 | `tools/base.py` + `ToolRegistry` + `tools/builtin/` 迁移 6 个核心 read tool(get_quote / get_history / ...) | 旧 9+ 测试套件全不破 + 加新 tool 不改核心代码 demo |
| **P4** | 1 天 | `core/orchestrator.py`(Tier 2 StateGraph 5 节点)+ `core/tier.py`(D1 三档路由)+ `core/short_circuit.py`(Tier 1 强执行) | 5 节点 plan-execute-verify-synthesize 测试 + 浏览器实测 80% query <3s |
| **P5** | 0.5 天 | `agents/` 5 个 sub-agent + `AgentRegistry` + `agents/base.py` | BaseAgent 测试 + AgentRegistry 测试 |
| **P6** | 0.5 天 | `plugins/base.py` + `PluginRegistry` + 3 个内置 plugin(Quant/News/Alert)+ entry_points 自动发现 demo | 加第三方 plugin 测试(可手写一个本地 plugin 验证) |
| **P7** | 0.5 天 | `observability/health.py` + `circuit_breaker` + `provider_failover` + health check endpoint + 飞书告警 webhook | 手工 kill 一个 provider,观察 failover 行为 + health check 返回正确状态 |

**总计 3-4 天**,预留 1 天 buffer 给测试和文档。

### 兼容性策略

| 时间 | 状态 |
|---|---|
| Day 1 (P1+P2+P3) | 新旧并存 — 旧 `tradingagents/agents/general/` 代码 re-export 到新 `tradingagents/agent_harness/`,旧 API 仍可用 |
| Day 2 (P4) | 旧代码开始 deprecate,新代码作为 primary |
| Day 3 (P5+P6) | 旧代码不再被引用,但仍保留(灰度迁移) |
| Day 4 (P7) | 旧代码删除(可选),新代码全量 |
| Day 5 | 文档更新 + commit/push/merge main + tag |

---

## 10. 不在本 spec 范围(明确划出去)

| 不做 | 理由 |
|---|---|
| Workflow 独立(D3) | 留 Day 15+,本 spec 只搭骨架 |
| 顶级独立包(选项 A) | 用户选 B 半独立,本 spec 不实现 |
| **8 层 Context Priority 完整实现(D4)** | **本 spec 只给接口,tier 1 实现简化版(只 explicit + conversation 两层)** | **v2 spec P4 (2026-09-13) 实施** |
| Multi-tenant / 多用户 | 单用户假设 |
| RAG(向量检索) | 留 Stage D |
| 模型 fine-tune | 不在 harness 层 |
| UI 改版 | 抽屉 UI 不变,只改 backend |

### 10.1 Context Priority 时机对齐(C3 fix,2026-09-11)

**v2 spec** 路线图 P4 = Context Priority 8 层完整实现
**v3 spec** 路线图无独立 Phase,仅 §7.1 D4 设计描述
**对齐方案**:v3 spec 不实施 Context Priority(由 v2 spec P4 负责),本 spec §7.1 D4 仅作设计参考。

---

## 11. 风险 & 拍板点

| # | Risk / Question | Mitigation / 待你拍板 |
|---|---|---|
| R1 | 旧代码 re-export 阶段容易出循环 import | **具体对策**(M5 fix,2026-09-11):<br>- **新代码 → 旧代码**:`TYPE_CHECKING` guard + `if TYPE_CHECKING: from ... import ...`<br>- **旧代码 → 新代码**:`__getattr__` lazy 加载(`__getattr__` 在模块级)<br>- **plugin → 旧代码**:`importlib.import_module` deferred(在 plugin install 时才 import)<br>- **测试验证**:`tests/test_harness_no_circular_import.py` 用 `importlib.import_module` 验证所有路径无循环 |
| R2 | 7 个 sub-agent 拆分可能粒度太细 | QuoteAgent 可考虑 merge 进 FundamentalsAgent(都数据查询),但保留更灵活 |
| R3 | Plugin entry_points 第三方生态短 | 内置 plugin 覆盖 80% 用例,第三方 plugin 留接口 |
| R4 | ToolRegistry 的 decorator + entry_points 双注册可能冲突 | 注册时去重,后注册抛错 |
| O14 | **7 个 sub-agent 划分**:太细 / 合适 / 太少? | **待你拍板**(下面) |
| O15 | **plugin 内置粒度**:Quant/News/Alert 3 个 / 还是更细? | **待你拍板**(下面) |
| O16 | **Day 1-4 立刻开干 vs 再 refine spec** | **待你拍板**(下面) |

### 11.1 待你拍板的 3 个决策

| # | 决策 | 选项 | 我推荐 |
|---|---|---|---|
| **O14** ✅ | 5 个 sub-agent 划分(合并 Quote+Fundamentals 为 DataAgent) | A 7 个 / **B 5 个** ✅ / C 9 个 | **B**(更聚焦,实现快 1 天) |
| **O15** ✅ | 3 个内置 plugin(Quant/News/Alert)| **A 3 个** ✅ / B 5 个 / C 1 个 | **A**(清晰,MCP 独立更好) |
| **O16** ✅ | 立即开干 vs 再 refine | **A 立即开干 Day 1-4** ✅ / B 再 refine spec 1-2 天 | **A**(spec 已够细,O14/O15 已拍板) |


### 11.2 MCP Server 状态澄清(C2 fix,2026-09-11)

**MCP server 实施状态**(对齐 v1/v2 spec 描述):

- **已完成**(2026-09-08 Day 5)— `tradingagents/agents/general/mcp_server.py`(289 行,暴露 15 tools)
- **v1 spec** O7 原本说"alpha + 5 core"范围(实际 Day 5 实施时扩到 15 tools)
- **v3 迁移策略**:MCP server 从 `tradingagents/agents/general/mcp_server.py` 迁移到 `tradingagents/agent_harness/mcp/server.py`
  - **不是** hardcoded 15 tools,而是**自动从 ToolRegistry 暴露所有 read tool**(write tool 需要 permission scope)
  - 迁移时间:**P6 阶段一并完成**(约 0.5 天)
  - 兼容期:旧 `general/mcp_server.py` 保留,新 `agent_harness/mcp/server.py` 加 `__main__` 入口可独立启动


---

## 12. Success Criteria

- [ ] `from tradingagents.agent_harness import Harness` 可用
- [ ] 旧 9+ 测试套件全不破(灰度迁移)
- [ ] 5 个 sub-agent(DataAgent 合并 QuoteAgent+FundamentalsAgent)全部实现 + 测试
- [ ] ToolRegistry + PluginRegistry + entry_points 验证通过
- [ ] Tier 1 短路径 10 个高频 query 端到端测试全过
- [ ] StateGraph 5 节点 plan-first retry verification 测试全过
- [ ] Health check endpoint 返回所有 provider/tool/agent 状态
- [ ] Circuit breaker 手工 kill 一个 provider 后自动 failover
- [ ] 加新 tool 不改 harness 核心代码(Plugin 机制验证)
- [ ] 实测 "600036 现在多少钱" 延迟 <3s,token=0
- [ ] merge 后打 `v0.7.1` tag(Day 12-15 增量)
- [ ] 文档齐全(spec + architecture + plugin 编写指南)

---

## 13. File Manifest

**新建**(50+ 文件):
- `tradingagents/agent_harness/{__init__.py, harness.py}` — 主入口
- `tradingagents/agent_harness/core/` — 编排核心 6 个文件
- `tradingagents/agent_harness/llm/` — LLM 适配 4 个文件
- `tradingagents/agent_harness/data/` — 数据层 8 个文件
- `tradingagents/agent_harness/tools/` — Tool 框架 5 个文件
- `tradingagents/agent_harness/memory/` — 分层记忆 4 个文件
- `tradingagents/agent_harness/agents/` — Sub-agent 7 个文件(O14=B 合并)
  - `base.py` / `registry.py` / `planner.py` / `synthesizer.py` / `verifier.py` / `data_agent.py` / `alpha_agent.py` / `news_agent.py`
- `tradingagents/agent_harness/tools/`(M6 fix,按 §3 目录扁平化分组)
  - 框架:`base.py` / `registry.py` / `schema.py` / `permission.py`
  - 内置 read tool:`builtin_quote.py` / `builtin_history.py` / `builtin_fundamentals.py` / `builtin_news.py` / `builtin_alpha.py`
  - 写 tool(HITL):`write_alert.py` / `write_note.py` / `write_scheduled.py`
- `tradingagents/agent_harness/workflow/` — Tier 3 DAG(留 Day 15+)
- `tradingagents/agent_harness/plugins/` — Plugin 系统 5 个文件
- `tradingagents/agent_harness/observability/` — 可观测 4 个文件
- `tradingagents/agent_harness/config/` — 配置 2 个文件
- `tradingagents/agent_harness/mcp/` — MCP server 1 个文件
- `web/routes/harness_health.py` — health check endpoint
- `tests/test_harness_*.py` — 全套测试

**改造**:
- `web/app.py` — 改 import 路径
- `web/routes/agent.py` — 改 import 路径
- `tradingagents/agents/general/` 各文件 → re-export 到新包,旧代码保留

**不变**:
- `web/static/` UI
- `dataflows/` 底层数据(被新 `data/providers/` 包装)
- 业务 agent(`quant/market/news` 等)

---

## 14. Next Steps

1. ⏸️ **等你拍板 O14 / O15 / O16**
2. ✅ P1: 创建 `agent_harness/` 骨架 + Harness 主类(0.5 天)
3. ✅ P2-P3: data + tools 迁移(1 天)
4. ✅ P4: orchestrator + tier 路由(1 天)
5. ✅ P5-P6: agents + plugins(1 天)
6. ✅ P7: observability + health check(0.5 天)
7. ✅ commit/push/merge main + tag v0.7.1

---

**附录**:关联文档
- v1 spec: `docs/superpowers/specs/2026-09-10-finance-general-agent-design.md`
- v2 spec: `docs/superpowers/specs/2026-09-11-finance-general-agent-harness-v2.md`
- v2 架构图: `docs/superpowers/specs/2026-09-11-finance-general-agent-harness-v2-architecture.md`
- 当前实现: `tradingagents/agents/general/{orchestrator,routing,prompts,tools_bridge,memory,audit,approval,mcp_server,guardrails}.py`
