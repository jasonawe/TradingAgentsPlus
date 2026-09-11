# 2026-09-11 — Finance General Agent Harness v2 (Harness Refactor Design)

**Date:** 2026-09-11
**Stage:** C Day 11d (增量设计,基于 v1 spec `2026-09-10-finance-general-agent-design.md`)
**Status:** 🔄 Draft — 待用户 review + 拍板
**Branch:** `codex/finance-general-agent`
**前置依赖:** 无(可立即基于现有代码开工)

---

## 0. TL;DR

**v1 设计已 100% 落地**(O5-O10 全部拍板 + 实现)。**Day 11 实战暴露 4 个底层架构问题,触发 v2 harness 重构**。v2 不替换 v1,而是**在 v1 基础上升级 harness 层**,新增 6 个设计决策(D1-D6)。

**核心问题一句话**:当前 agent 把"数据查询 + 深度分析 + 对话陪伴"三个角色塞进同一个 ReAct loop,导致哪个都做不好。

**v2 核心方案**:把三个角色**分开设计**,引入三档 Execution Model + Tool Pydantic 化 + 显式 StateGraph + Verification 体系。

---

## 1. Background — Why v2

### 1.1 v1 已完成的工作

2026-09-10 spec 设计的 7 条原则 + 6 个决策(O5-O10)已全部落地:
- ✅ MCP server(Day 5,15 tools)
- ✅ 真实 LLM 集成(Day 4)
- ✅ Drawer UI(Day 7-11,已修 click + 渲染 bug)
- ✅ Routing 关键词加速 + LLM fallback(Day 8)
- ✅ L1/L2/L3 分层记忆(Day 9)
- ✅ 6 新 tool + Stage C audit UI(Day 7-9)

### 1.2 Day 11 实战暴露的 4 个问题

**问题 A:ReAct loop 不主导流程,LLM 自由发挥导致"牛头不对马嘴"**

实测 `600036 现在多少钱?` 这种 80% 高频 query,LLM 倾向输出"我是 TradingAgents 通用 Agent,请问..."等欢迎语,**不调 tool**。根本原因是 `create_react_agent` 把 LLM 当主导,harness 没有强执行。

**问题 B:Tool 层没强 schema**

`tools_bridge.py` 21 个 LangChain `@tool` 函数,入参是 `BaseModel` 但**没有 server-side validate**。LLM 可以传任意 dict,出错了静默。

**问题 C:`run_trading_agents_analysis` 不该走 chat loop**

深度分析任务(多 agent pipeline)被当作"tool call"塞进 ReAct loop,实际上它是 workflow DAG。混在一起导致:
- 进度不可见
- 失败无法 partial-retry
- 状态没有显式存储

**问题 D:无 verification / retry / plan 体系**

- tool_call=0 不报错 → LLM 继续幻觉
- tool_result 错误不重试 → 用户看到错误数据
- LLM 不告诉用户它打算干嘛 → 用户体验"黑盒"
- 长对话 context 爆炸 → 后期 LLM 重复调 tool

### 1.3 v2 设计目标

| # | 目标 | 验证方式 |
|---|---|---|
| G1 | 80% 高频 query 走 server-side 强执行,延迟 <3s,token=0 | 端到端测试覆盖 10 个高频 query |
| G2 | 写操作走显式 state machine,不再用字符串 marker | 测试覆盖所有写 tool |
| G3 | 深度分析任务走独立 workflow DAG,不再混在 chat loop | `run_trading_agents_analysis` 拆出来 |
| G4 | LLM 回答前必须先 verify tool_result + answer grounded | 新增 verification node |
| G5 | Plan 可见(用户能在 UI 看到 agent 打算干嘛) | SSE 推送 plan 事件 |

---

## 2. v2 设计决策 (D1-D6)

### D1. 三档 Execution Model(角色切分)

**核心 insight**:理财 agent 不是单一角色,而是 3 个角色的组合。

```
Tier 1: Direct Tool (server-side, 无 LLM)
  适用:"600036 多少钱" / "我的关注列表" / "RSI 多少"
  路径:regex extract ticker → intent classify (keyword) → tool.invoke() → 合成回答
  LLM 调用:0 次
  延迟:<1s

Tier 2: Plan + Execute (LLM plan + server execute)
  适用:"分析 600036 估值合理性" / "对比 600036 跟 600000"
  路径:LLM 给 JSON plan → server 按 plan 顺序/并行调 tool → synthesize
  LLM 调用:**2-3 次**(plan + synthesize;启用 L3 verification 时 +1 次 LLM-judge,默认关 — N16 fix,2026-09-11)
  延迟:5-10s(N17 fix,2026-09-11,与架构文档 §3 fig 2 example 对齐)
  token:**500-1200**(plan 看到 4 个 tool schema ≈800 + synthesize ≈400 — N21 fix,2026-09-11)

Tier 3: Full Workflow (multi-agent DAG)
  适用:"深度分析 600036 全维度" / "批量回测 ETF 动量"
  路径:独立 workflow orchestrator(已有 TradingAgents 主图)
  LLM 调用:5-15 次
  延迟:30s-5min
```

**Tier 路由规则表**(C3 fix,2026-09-11):

| 触发条件 | Tier | 路径 | LLM 调用 |
|---|---|---|---|
| keyword 命中"价格/多少钱/报价/quote/RSI/换手" + ticker 抽取成功 | **Tier 1** | regex → tool.invoke() → emit raw data | 0 次 |
| keyword 命中"估值/对比/分析" + ticker 抽取成功 | **Tier 2** | LLM plan → server 执行 → LLM synthesize | **2-3 次**(L3 默认关) |
| keyword 命中"深度/综合/详细" + ticker 数量 = 1 + LLM 推理需要 | **Tier 2** | LLM plan → server 执行 → LLM synthesize | **2-3 次**(L3 默认关) |
| keyword 命中"深度/综合/详细" + ticker 数量 > 1(对比/批量) + LLM 推理需要 | **Tier 3** | 多 agent DAG 并行(Workflow runner) | 5-15 次 |
| keyword 命中"价格/多少钱" 但 ticker 抽取失败(用户没说标的) | **Tier 1 → 降级 Tier 2** | regex miss → fallback 到 LLM 反问 ticker | 1 次(反问) |
| 写操作(create_*/update_*/delete_*) | 强制 HITL | ConfirmNode yield → 等用户 confirm → execute | 0-1 次 |

**Fallback 路径**(某 tier 失败时):Tier 1 失败 → Tier 2 / Tier 2 失败 → Tier 3 / 全部失败 → 返回错误 + 提示用户换 query。

**Tier 1 子模式**(M2 fix,2026-09-11):

| 子模式 | 行为 | LLM 调用 | 适用场景 |
|---|---|---|---|
| **Tier 1a** raw emit | regex → tool.invoke() → emit 数据 → 前端渲染结构化卡片 | 0 | 简单数据查询(价格/历史/因子) |
| **Tier 1b** template 合成 | regex → tool.invoke() → Jinja2 模板拼文字回答 → emit | 0 | 数据 + 简短文字说明(如"RSI 67 偏高") |

默认 Tier 1a,UI 渲染结构化数据卡片(更精确);Tier 1b 由 query 关键词("说明"/"解释")触发。两者都 0 LLM。

**落地**:
- `stream_chat` 入口加 **tier classification**(N44 fix,2026-09-11):当前 routing.py 已有 `fast_route() / classify_intent() / render_intent_hint()`(返回 RouteResult(Intent, confidence, reason));P1 实施时**扩展** `classify_intent` 增加 tier 维度(Tier 1/2/3),**不**新增 `classify_tier` 函数(避免双入口)。修改路径: 加 tier 字段 → `orchestrator.py:stream_chat` 根据 tier 选择 Tier 1 short_circuit / Tier 2 StateGraph / Tier 3 workflow
- Tier 1 → server-side 直接调 tool + emit 完整事件链 + 跳过 LangGraph

**ticker 抽取说明**(N53 fix,2026-09-11):§D1 路由表中的 "ticker 抽取成功/failed" 由 `routing.py:fast_route()` 内部 inline 处理(使用 LangChain `tool_call` 的 args.symbol 或 regex `\b[A-Z]{1,5}(\.[A-Z]{2})?\b`),**没有**独立 `extract_ticker()` 函数。P1 实施时**不**新增 ticker extractor 函数,直接复用 fast_route 内部的 ticker 解析逻辑(从 fast_route 返回 RouteResult 加 ticker 字段)。
- Tier 2 → 进新的 StateGraph(见 D5)
- Tier 3 → 调现有 `run_trading_agents_analysis` workflow

### D2. Tool 重构 — Pydantic Schema + Unified DataResponse

**借鉴**:OpenBB OBBject + Provider ABC + WrenAI sqlglot 校验

**改动**:
- 新建 `data/providers/base.py`:定义 `Provider` ABC(统一 `get_quote / get_history / get_fundamentals` 接口)
- 新建 `data/providers/registry.py`:`PROVIDERS = {"yfinance": ..., "eastmoney": ..., "akshare": ..., "alpha_vantage": ...}`(N40 fix,2026-09-11,实际 4 个 provider)
- 新建 `data/responses.py`:统一 `DataResponse(BaseModel)` 含 `results / provider / fetched_at / warnings / chart`
- 改造 `tools_bridge.py`:每个 tool 入参改 Pydantic schema + server-side validate

**Pydantic 版本**:v2 (`from pydantic import BaseModel, Field`),如果项目还在 v1 先升级。

**向后兼容策略**(C2 fix,2026-09-11):
- **过渡期 3-5 天**:旧 LangChain `@tool` 函数保留,新 tool 用 Pydantic BaseModel 入参
- **混用原则**:Pydantic 化时**只**改入参 schema,不改方法体
- **迁移完成标准**:全部 21 个 tool 都有 v2 版本,然后删除 v1 版本

**示例**:
```python
# Before (LangChain @tool)
@tool
def get_quote(symbol: str, asset_type: str = "stock") -> dict:
    """获取单个标的最新行情"""
    return quote_service.get_quote(symbol, asset_type)

# After (Pydantic schema + DataResponse)
class QuoteArgs(BaseModel):
    symbol: str = Field(..., description="ticker code, e.g. 600036.SS")
    asset_type: str = Field("stock", pattern="^(stock|etf|index|crypto)$")

class QuoteResult(DataResponse):
    results: QuoteData  # nested Pydantic

@tool(args_schema=QuoteArgs)
def get_quote(symbol: str, asset_type: str = "stock") -> dict:
    """获取单个标的最新行情"""
    args = QuoteArgs(symbol=symbol, asset_type=asset_type)  # validate
    data = PROVIDERS[get_active_provider()].get_quote(args.symbol, args.asset_type)
    return QuoteResult(
        results=data,
        provider=get_active_provider(),
        fetched_at=datetime.utcnow(),
        warnings=[],
    ).model_dump()
```

### D3. Workflow 独立 — `run_trading_agents_analysis` 走独立 orchestrator

**借鉴**:OpenBB Quantly playbooks + Anthropic Claude Code sub-agent dispatch

**现状问题**:`run_trading_agents_analysis` 在 `tools_bridge.py` 里被当 `@tool` 注册,LLM 调它后走的是 LangGraph 默认 sub-graph,但没有显式 workflow 编排。

**改动**:
- 抽出 `tradingagents/agents/workflow/` 子目录(Day 12-15 实施)
- 新建 `workflow/runner.py`:独立 workflow orchestrator,接收 `WorkflowRequest`(ticker + analysis_type + params)
- 不复用 chat loop,有自己的:
  - Progress 推送(SSE `workflow_progress` 事件)
  - Partial retry(node 失败可单独重试)
  - State 持久化(L4 workflow_run state table)
  - Plan 可见(DAG node 列表)

### D4. Explicit Context Priority

**借鉴**:OpenBB 8 层 context priority

```
1. Explicit widgets (用户在 UI 选中的 ticker / 时间范围)
2. Skills (MCP skill / 预设能力)
3. MCP tools (tool schemas)
4. Files (上传的 PDF / CSV)
5. Dashboard state (watchlist / 当前 tab)
6. Conversation (L1 short-term)
7. Global (L2 preferences)
8. Web Search (L3 reference)
```

**冲突解决规则**(C4 fix,2026-09-11):
- **优先级**:数字越小优先级越高(Explicit > Skills > ... > Web Search)
- **冲突解决**:高优先级**覆盖**低优先级(不是合并)
- **Token 预算**(默认 4K total):
  - Layer 1-2:各 200 tokens
  - Layer 3 tool schemas:动态剪枝,上限 800 tokens
  - Layer 4-5:各 300 tokens
  - Layer 6:**≤1500 tokens 上限**(N5 fix,2026-09-11)。最近 5 轮对话全量,超过则先 summarize 最老 1-2 轮再 trim。**单层超 1500 → 立即触发 summarize,不能等总预算超 4K**
  - Layer 7:200 tokens
  - Layer 8:仅当其他层不够时启用,上限 500 tokens
- **Trim 触发**:total > 4K 时,从 Layer 8 往上 trim,直到 < 4K

**实施时机**:v2 spec P4 (2026-09-13)。

**改动**:
- 新建 `agent/context/priority.py`:定义 8 层优先级 + 注入规则
- `stream_chat` 入口根据优先级拼装 context,而不是 LLM 自由从 system prompt 提取
- 每层有显式注入规则(注入多少 token / 哪些字段 / 何时 trim)

### D5. StateGraph 主图 5 节点(Tier 2 主图)— 实施时另有 12+ sub-state

**借鉴**:LangChain DeepAgents TodoMiddleware + Anthropic Claude Code plan-first

**新 StateGraph**:
```
PlanNode → ExecuteNode → ObserveNode → VerifyNode → SynthesizeNode
    ↑          ↓                              ↓
    └──────── retry (max 3 + backoff) ────────┘
```

**注**(N20 fix,2026-09-11):上面是**主图 5 节点**(plan/execute/observe/verify/synthesize)。实施时 fig 2 实际有 17+ state,包括以下 sub-state:
- **验证子图**:CheckToolCall(L1) / CheckResult(L2) / CheckAnswer(L3) / AutoBump(tool_call=0 触发)/ RetryExecute / BackToPlan
- **HITL 子图**:ConfirmNode(写操作)/ CheckWriteOp / WaitUser(等用户 approve)/ ExecuteWrite
- **Emitter 子图**:EmitFinal(SSE final event)

"5 节点" 是营销命名,实施者必须按 fig 2 的 17+ state 全量实现,不能漏 sub-state。

**节点职责**:

| Node | 职责 | LLM 调用 | 实现 |
|---|---|---|---|
| **PlanNode** | LLM 给 JSON plan `[{"step": 1, "action": "tool_name", "args": {...}}]` | 1 次 | JSON parser 强校验 |
| **ExecuteNode** | server 按 plan 顺序调 tool(`asyncio.gather` 并行) | 0 次 | Pydantic validate + invoke |
| **ObserveNode** | 汇总 tool_result,生成 intermediate state | 0 次 | server logic |
| **VerifyNode** | tool_call=0 → auto-bump LLM "请说明为什么不调 tool";tool_result 错误 → auto-retry | 0-1 次 | server logic + 必要时调 LLM |
| **SynthesizeNode** | 基于已 verified 数据生成自然语言回答 | 1 次 | LLM call |
| **ConfirmNode**(写操作) | 触发 HITL confirm dialog,用户 approve 后再 execute | 0 次 | 替换现有 AWAITING_CONFIRMATION 字符串 |

**Plan 可见**:SSE 推送 `plan_ready` 事件,UI 显示 "Step 1/4: fetch quote..."

### D6. Verification 体系(三层)

**借鉴**:Anthropic Claude Code test/visual/LLM-judge 三层验证 + Codex CLI sandbox verification

| 层 | 检查 | 失败时 |
|---|---|---|
| **L1 tool_call** | tool_call 数量 > 0;args 符合 schema | auto-bump LLM 重生成 |
| **L2 tool_result** | 数据结构符合 schema;关键字段非空;数据新鲜度 < TTL | auto-retry(指数 backoff,max 3) |
| **L3 answer** | LLM-judge 评估 groundedness("回答是否基于 tool_result") | score < 0.7 → back to PlanNode |

**实现**:
- `verification/tool_call.py` — L1
- `verification/tool_result.py` — L2
- `verification/answer_judge.py` — L3(用同一个 LLM 做 judge)

**L3 LLM-judge schema**(C5 fix,2026-09-11):

```python
class LLMJudgeVerdict(BaseModel):
    score: float = Field(..., ge=0.0, le=1.0, description="groundedness 评分,>= 0.7 视为 grounded")
    issues: list[str] = Field(default_factory=list, description="具体问题(数字不准 / 编造 / 答非所问)")
    suggestion: str = Field("", description="改进建议(回 plan / 调更多 tool / 调整 query)")
    reasoning: str = Field("", description="judge 的推理过程")
```

**Judge Prompt 模板**:
```
你是 answer-groundedness judge。评估以下 LLM 回答是否基于 tool_result(而不是幻觉)。
user_query: {user_query}
tool_results: {tool_results_summary}
llm_answer: {llm_answer}
输出 JSON: { "score": 0.0-1.0, "issues": [...], "suggestion": "...", "reasoning": "..." }
```

**0.7 阈值依据**(经验值,需 pilot 校准):
- 类似系统内部数据,0.7 是 grounded vs hallucinated 常见分界点
- Pilot 校准:P3 后跑 10-20 个 query,人工标 ground-truth,微调

**L3 默认关闭,UI 加 toggle**(O13 拍板):
- 关闭:跳过 L3,L1+L2 通过即 verified
- 开启:额外 ~500 token,质量 +2-3x

---

## 3. 借鉴来源(决策映射)

| v2 决策 | 主要借鉴 | 次要借鉴 |
|---|---|---|
| D1 三档 Execution | OpenBB MCP-first + WrenAI guided/direct | FinMem 单/多 agent |
| D2 Tool Pydantic + DataResponse | **OpenBB OBBject + Provider ABC** | FinRobot schema prompt |
| D3 Workflow 独立 | **OpenBB Quantly playbooks** | Anthropic Claude Code sub-agent |
| D4 Context Priority | **OpenBB 8 层优先级** | WrenAI schema_items |
| D5 StateGraph 主图 5 节点(实施时 17+ sub-state) | **LangChain DeepAgents TodoMiddleware** + Anthropic Claude Code plan | FinMem observation/thinking |
| D6 Verification | **Anthropic Claude Code LLM-judge** + Codex CLI sandbox | WrenAI sqlglot AST |

**核心 takeaway(贯穿 D1-D6)**:从 Anthropic / DeepAgents 学到的两条铁律
1. **"Harness controls the loop, LLM does the judgment"** — D1/D2/D5/D6 全是 harness 接管 LLM 自由
2. **"LLM coordinates algorithms; it doesn't replace them"** — D2/D4/D6 把算法活从 LLM 拿回 server

---

## 4. 实施 Roadmap

| Phase | 时间 | 内容 | 验证 | Day |
|---|---|---|---|---|
| **P0** | 半天 | 本 doc review + 用户拍板 | 用户确认 D1-D6 | Day 11d |
| **P1** | **4-5h**(M1 fix) | **Tier 1 Direct Tool** + **Provider ABC** + **DataResponse** | 10 个高频 query 端到端测试 + TestClient | Day 12 |
**P1 回归测试**:全套 9+ tests/test_d*.py 不破(M4 fix) |
| **P2** | 6-8h | **StateGraph 主图 5 节点(17+ sub-state)** + **Context Priority** + **Verification L1+L2** | plan-first + retry + verification 测试 + 浏览器实测 | Day 13 |
| **P3** | 半天 | **Workflow 独立** + **Verification L3**(LLM-judge) | `run_trading_agents_analysis` 单独跑通 | Day 14 |
| **P4** | 半天 | 文档 + README 更新 + commit/push/merge | 文档齐全 + merge main | Day 14 收尾 |

**每个 Phase 独立可 ship**,失败可回滚。

---

## 5. 不在 v2 范围(明确划出去)

| 不做 | 理由 |
|---|---|
| UI 重大改版 | 当前 drawer + 折叠 trace 已够用,优先改 backend |
| 多 agent 派 sub-agent | 留 Day 15+,先把单 agent 跑通 |
| 真实交易下单 | 永远不做(决策辅助 only) |
| RAG(向量检索历史报告) | 留 Stage D |
| 模型 fine-tune | 优先 prompt + harness,不动模型 |
| 跨用户/多租户 | 单用户假设 |
| 多模态(图/语音) | 文本优先 |

---

## 6. 风险 & 拍板点

| # | Risk / Question | Mitigation / 待你拍板 |
|---|---|---|
| **R1** | Pydantic 化改造量大(21 个 tool) | 先迁移 5 个核心 tool,其余渐进 |
| **R2** | StateGraph 重构会破坏现有测试 | P2 单独 PR,失败可回滚到 v1 |
| **R3** | LLM-judge 增加 token 成本 | **L3 默认关闭**(§D6 O13 拍板),UI 加 toggle;仅当 tool_result > 4 个 且用户主动开启时才启用(否则 0 额外 token)。参考 §D6 完整设计。 |
| **R4** | 用户可能不喜欢 plan 可见 UI | 默认折叠,用户主动展开 |
| **O11** | **D1-D6 6 个决策哪些做 / 优先级** | **待你拍板**(下面) |
| **O12** | **D3 Workflow 独立**:要不要在 v2 范围 | 复杂度高,可能留 Day 15 |
| **O13** | **D6 L3 LLM-judge**:开 / 关 | 开 = 质量 +2-3x 但成本 +30% |

### 6.1 待拍板的决策矩阵(精简版)

| # | 决策 | 选项 | 推荐 |
|---|---|---|---|
| **O11** | D1-D6 全做 vs 优先级 | A 全做 / B 只 D1+D2+D5 / C 只 D1 | **B**(性价比最高) |
| **O12** | D3 Workflow 独立 | 1 v2 做 / 2 留 Day 15+ | **2**(留后续) |
| **O13** | D6 L3 LLM-judge | 1 开 / 2 关 / 3 默认关可选开 | **3**(用户可控) |

---

## 7. Success Criteria

- [ ] Tier 1 短路径 10 个高频 query 端到端测试全过
- [ ] StateGraph 主图 5 节点 + 17+ sub-state plan-first retry verification 测试全过
- [ ] 现有 9+ 测试套件全不破(向后兼容)
- [ ] 实测 "600036 现在多少钱" 延迟 <3s,token=0,bubble 显示真实价格
- [ ] 实测 "分析 600036 估值合理性" plan 可见,user 可看到进度
- [ ] 实测深度分析走 workflow DAG,不再串进 chat loop
- [ ] 不破坏现有 MCP server / Drawer / 折叠 trace
- [ ] merge 后打 `v0.7.1` tag(Day 12-14 增量)

---

## 8. 与 v1 spec 的关系

| v1 spec 已拍板 | v2 升级 / 新增 |
|---|---|
| O5 UI 抽屉 | ✅ 不变 |
| O6v2 写操作需 confirm | **升级** → D5 ConfirmNode 显式 state machine |
| O7 MCP 范围 | ✅ 不变 |
| O8 Memory 默认 off | ✅ 不变 |
| O9 Reasoning 折叠 | ✅ 不变 |
| O10 Audit log | ✅ 不变 |
| 7 条设计原则 | **保留全部 + 新增 2 条**:"Harness controls loop" / "LLM coordinates algorithms" |
| Scope / Non-goals | ✅ 不变(MVP scope 不扩) |

---

## 9. File Manifest(v2 增量)

**新建**:
- `data/providers/base.py` — Provider ABC
- `data/providers/registry.py` — PROVIDERS dict
- `data/responses.py` — DataResponse / QuoteData / HistoryData / FundamentalsData
- `agent/context/priority.py` — 8 层 context priority
- `verification/tool_call.py` — L1 verification
- `verification/tool_result.py` — L2 verification
- `verification/answer_judge.py` — L3 LLM-judge
- `agent/tier.py` — D1 三档 execution 路由
- `agent/short_circuit.py` — Tier 1 server-side 直调
- `agent/stategraph.py` — D5 StateGraph 主图 5 节点(17+ sub-state)
- `tests/test_d12_provider_abc.py`
- `tests/test_d12_tier1_short_circuit.py`
- `tests/test_d13_stategraph.py`
- `tests/test_d13_verification.py`

**改造**:
- `tools_bridge.py` — Pydantic 化(D2 渐进迁移,先 5 个核心)
- `orchestrator.py` — 接入 tier routing + 新 StateGraph
- `prompts.py` — 新增 PlanPrompt / VerifyPrompt / SynthesizePrompt
- `routing.py` — 升级 classify_intent → classify_tier(返回 tier + intent)

**不变**:
- `mcp_server.py` / `memory.py` / `audit.py` / `approval.py` / Drawer UI

---

## 10. Next Steps

1. ⏸️ **等你 review + 拍板 O11 / O12 / O13**
2. ✅ P1: 立即开工 Day 12(**4-5h**,M1 fix 调整)
3. ✅ P2: Day 13(6-8h)
4. ✅ P3 + P4: Day 14(半天)
5. ✅ 文档更新 + merge + tag v0.7.1

**如果你认同大方向 + O11 选 B + O12 选 2 + O13 选 3,我立即开 P1(Day 12)**。

**如果你想看更细的设计**(具体 plan 格式 / verify 阈值 / StateGraph 节点 schema),告诉我,我深入展开。

---

**附录**:关联文档
- v1 spec: `docs/superpowers/specs/2026-09-10-finance-general-agent-design.md`
- 5 项目 deep dive: `docs/superpowers/research/2026-09-10-*.md`
- 当前实现: `tradingagents/agents/general/{orchestrator,routing,prompts,tools_bridge,memory,audit,approval,mcp_server}.py`
