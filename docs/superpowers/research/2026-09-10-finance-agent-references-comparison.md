# 理财通用 Agent 参考项目横向对比

> 研究日期:2026-09-10  
> 目的:基于 5 个开源项目调研,为 TradingAgents Stage C (理财通用 Agent) 提供**横向决策依据**  
> 关联文档:`2026-09-10-finance-general-agent-design.md`(Stage C 主设计)

---

## 1. 5 个项目一览

| 项目 | 类型 | License | 规模 | 对 Stage C 价值 |
|------|------|------|------|------|
| **FinRobot** | 多 Agent 投资分析框架 | MIT | 8 Agents + 框架 | ⭐⭐⭐⭐ 架构 + 强 schema prompt |
| **WrenAI** | GenBI Text-to-SQL 引擎 | Apache-2.0 | Rust core + Python | ⭐⭐⭐⭐⭐ UI / MDL / Policy |
| **FinMem** | Agent + 分层记忆 | MIT | 论文+PoC | ⭐⭐⭐⭐ 记忆机制 |
| **OpenBB** | 统一数据平台 | **AGPL-3** | 100+ providers | ⭐⭐⭐ 数据层抽象(只学不用) |
| **Qlib** | 量化研究平台 | MIT | 完整 ML pipeline | ⭐⭐⭐ 因子/回测架构 |

**结论**:5 个项目都是 MIT/Apache 友好,只有 OpenBB 是 AGPL(强 copyleft)— **绝对不能直接 fork OpenBB 代码**,只能学架构。

---

## 2. 横向对比矩阵

### 2.1 Agent 架构

| 项目 | Agent 编排 | 多 Agent 真实度 | LangGraph 兼容 |
|------|------|------|------|
| FinRobot | 串行 + Legacy group-chat | **半真** — equity 栈是"多 prompt 串行",只有 legacy 是真 group-chat | 需适配 |
| WrenAI | **无内置 agent** — Agent-in-the-loop | N/A(外部 agent) | ✅ 完全兼容(我们当外部 agent) |
| FinMem | 单 Agent observation/thinking/decision | 单 agent 循环 | ✅ 容易集成 |
| OpenBB | 无 agent — 数据中介 | N/A | N/A |
| Qlib | 无 agent — qrun YAML workflow | YAML 反射 | 需转换 |

**启示**:**WrenAI 模式 + LangGraph 内部编排** 是最干净的选择 — 我们 Stage C 内部用 LangGraph 编排,核心 tools 也通过 CLI/MCP 暴露给外部 agent。

### 2.2 记忆机制

| 项目 | 分层 | 存储 | Embedding |
|------|------|------|------|
| FinRobot | **无** — 只有 session 表 | SQLite | N/A |
| WrenAI | **2 层**:schema_items + query_history | **LanceDB** | paraphrase-multilingual-MiniLM-L12-v2 |
| FinMem | **L1 short-term / L2 long-term / L3 reflective** | 文本 + summary | OpenAI embedding |
| OpenBB | 无业务记忆 — 只有 cache | 磁盘(SQLite/Parquet) | N/A |
| Qlib | 无 — 实验跟踪用 MLflow | MLflow | N/A |

**启示**:**WrenAI + FinMem 混合方案最对标我们 Stage C 的 L1/L2/L3**:
- **L1 LangGraph MemorySaver**(短期对话)— LangGraph 自带
- **L2 schema_items 向量化**(业务语义)— 抄 WrenAI 用 LanceDB
- **L3 query_history**(历史引用)— 抄 WrenAI schema + FinMem 反思机制

### 2.3 工具系统

| 项目 | 工具数 | 注册方式 | MCP 暴露 |
|------|------|------|------|
| FinRobot | ~15 (财报 / 行情 / 新闻 / SEC) | 散落各处,无 registry | ❌ |
| WrenAI | wren ask / memory / mdl / serve / dbt | Typer CLI | 间接通过 skills/ |
| FinMem | 5 (行情 / 财报 / 新闻 / 推特 / SEC) | 函数直接调 | ❌ |
| OpenBB | 100+ providers | entry_points + decorator | ✅ **OpenBB MCP Server 是 first-class** |
| Qlib | 数据 / 模型 / 策略 / 回测 | YAML config + class path | ❌ |

**启示**:**Stage C 必做 MCP server**,参考 OpenBB 的 MCP server 实现(他们已经在产线用了)。

### 2.4 数据 / 工具治理

| 项目 | 写操作拦截 | SQL 治理 | Provider 抽象 |
|------|------|------|------|
| FinRobot | ❌ | N/A(纯 LLM 工具) | ❌ |
| WrenAI | ✅ **三层防线**(read-only / strict / file-reader 黑名单) | ✅ sqlglot AST 校验 | 22+ 数据源统一 |
| FinMem | ❌ | N/A | ❌ |
| OpenBB | N/A | N/A | ✅ **Provider ABC + registry + 100+ adapter** |
| Qlib | N/A | N/A | 数据源接入层 |

**启示**:
- **写操作拦截**:抄 WrenAI 三层防线
- **Provider 抽象**:抄 OpenBB 的 Provider ABC + registry(但**只做 3-5 个 provider**)
- **不要学 OpenBB 100+ providers 模式**(过度抽象)

### 2.5 UI / 交互

| 项目 | 前端 | Chat UI | SSE 流式 |
|------|------|------|------|
| FinRobot | Vue 3 + Tailwind(传统表单)| ❌ 没有 chat | ❌ 1 秒轮询 |
| WrenAI (legacy/v1) | Chat-first BI(Docker) | ✅ chat + chart | ✅ |
| WrenAI (新版) | **无内置 UI**(给外部 agent) | N/A | N/A |
| FinMem | CLI 为主 | ❌ | ❌ |
| OpenBB | Workspace (TS + Electron) | ✅ chat(Workspace) | ✅ |
| Qlib | Jupyter Notebook | N/A | N/A |

**启示**:**Stage C 的抽屉式 chat agent UI** 直接参考 WrenAI legacy/v1 + OpenBB Workspace 的 chat widget。

### 2.6 量化研究 / 因子

| 项目 | 因子库 | 回测引擎 | ML 模型 |
|------|------|------|------|
| FinRobot | 无 | 无 | ❌ |
| WrenAI | 无 | 无 | ❌ |
| FinMem | 无 | 简单 | ❌ |
| OpenBB | 无 | 简单 | ❌ |
| **Qlib** | **Alpha158 + Alpha360**(配置驱动) | ✅ **Exchange**(完整滑点/手续费) | LightGBM / LSTM / Transformer 全套 |

**对我们 B 系列启示**:
- **B1 Alpha158** — Qlib 的 6 分组模板 + `FACTOR_REGISTRY` 字典值得抄(我们 B1 是手写工厂函数)
- **B2 paper_account** — Qlib Exchange 默认参数(open_cost=0.0015, close_cost=0.0025, min_cost=5)可参考
- **B3 BarGenerator** — 我们已经做了,跟 Qlib 一致
- **B4 上 ML 模型** — 抄 Qlib 的 qrun YAML workflow + MLflow tracking

---

## 3. Stage C 决策矩阵(更新版)

结合 5 个项目调研 + IBM 文章启示,更新原 Stage C 设计的 6 个决策:

| # | 决策 | 选项 | **调研后建议** | 依据 |
|---|------|------|------|------|
| **O5** | UI 形态 | A 抽屉 / B 全屏 / C 浮窗 | **A 抽屉** | WrenAI legacy/v1 + OpenBB Workspace 都用类似 chat widget |
| **O6** | 写操作权限 | 1 只读 / 2 读+写 / 3 + 触发分析 | **O6v2: A 读+写需 confirm** | WrenAI 三层防线 + IBM 文章启示 |
| **O7** | MCP 范围 | 1 只 alpha / 2 alpha+5 core / 3 全 12+ | **2 alpha+5 core** | OpenBB MCP server 范式 + 8 tool 足够覆盖 80% 用例 |
| **O8** | Memory 默认 | on / off | **off**(隐私) | WrenAI 默认 ~/.wren/memory,用户可控 |
| **O9** | Reasoning trace UI | A SSE 流显示推理 / B 折叠面板 | **B 折叠面板** | WrenAI "reasoning 在 trace 里",不打扰主流程 |
| **O10** | Audit log | A 必做 / B 留接口 | **A 必做** | IBM 文章 + WrenAI sync_markdown_queries 的 audit 模式 |

---

## 4. Stage C 实施优先级(基于调研调整)

### 第一梯队(MVP,5-7 天)— 必做

| 模块 | 来源 | 工作量 |
|------|------|------|
| **MCP server** | OpenBB 范式 | 2 天 |
| **统一 Provider 抽象** | OpenBB Provider ABC | 1 天(我们已经 3 个 provider,只需抽 base) |
| **MDL 语义层** | WrenAI MDL 简化版 | 1 天 |
| **L1 MemorySaver** | LangGraph 内置 | 0.5 天 |
| **L2 schema_items 向量化** | WrenAI LanceDB | 1 天 |
| **Policy 三层防线** | WrenAI policy.py | 1 天 |
| **SSE agent drawer UI** | WrenAI legacy/v1 + OpenBB Workspace | 1.5 天 |
| **Audit log + write confirm** | IBM 文章 + WrenAI | 0.5 天 |

### 第二梯队(增强,3-5 天)

| 模块 | 来源 | 工作量 |
|------|------|------|
| **L3 query_history** | WrenAI + FinMem 反思 | 2 天 |
| **Reasoning trace 折叠面板** | WrenAI trace | 1 天 |
| **CLI `ta ask` 暴露** | WrenAI ask CLI | 1 天 |
| **MDL watch 自动重建** | WrenAI memory watch | 0.5 天 |
| **OpenBB-style Provider 注册表** | OpenBB PROVIDERS dict | 0.5 天 |

### 第三梯队(后续 Stage D-E)

- 多用户协作 / Team workspace(OpenBB Workspace)
- B2 paper_account(Qlib Exchange 范式)
- B4 ML 模型 pipeline(Qlib qrun 范式)
- vnpy 实盘对接(国内合规)

---

## 5. Stage C 设计文档更新点

需要在 `2026-09-10-finance-general-agent-design.md` 补充:

1. **架构图加入 WrenAI "3 大支柱"** — MDL / Memory / Policy
2. **tools_bridge.py 工具列表对齐 O7** — alpha158 ×3 + core ×5 + write ×4 = 12 tools
3. **memory.py 加入 LanceDB** — `~/.tradingagents/agent_memory/`(模仿 WrenAI 的 `~/.wren/memory/`)
4. **新增 `mcp_server/server.py`** — fastmcp,暴露 8 tool(对齐 O7 选项 2)
5. **新增 `agent/mdl/stock_market.yaml`** — 简化版 MDL,声明 stock_daily / stock_basic / watchlist
6. **新增 `guardrails.py`** — 写操作拦截 + max_tool_calls 限制(对应 O6v2)
7. **新增 `audit.py`** — write_audit_log 写入 + 查询(对应 O10)
8. **web/static/agent.js 新增** — confirmation dialog + reasoning trace panel + data_quality 标记

---

## 6. 风险汇总 + 缓解

| 风险 | 来源 | 缓解 |
|------|------|------|
| **AGPL 污染** | OpenBB | 只学架构,代码 0 行复制,license header 不引入 |
| **Rust core 编译复杂** | WrenAI wren-core | 我们用 Python + SQLite + LanceDB,不上 Rust |
| **MDL YAML 维护成本** | WrenAI MDL | 简化版,只覆盖核心 5-8 个 table,后续按需扩 |
| **LanceDB 嵌入式限制** | WrenAI Memory | 单机够用,分布式场景再换 qdrant |
| **Legacy v1 vs 新版分裂** | WrenAI 文档 | 重点看新版的 ask_cli / policy / memory,UI 参考 legacy/v1 |
| **provider 数据一致性** | OpenBB | OBBject 里标识 source + fetched_at,前端展示 |
| **quant 复杂度** | Qlib | B2/B4 按需引入,不一次到位 |

---

## 7. 参考 deep-dive 索引

- `2026-09-10-finrobot-deep-dive.md` (258 行) — 多 Agent 框架 / 强 schema prompt / **缺点:无 SSE / 无 chat / 无记忆**
- `2026-09-10-wrenai-deep-dive.md` (269 行) — **MDL + Memory + Policy 三件套** / Agent-in-the-loop
- `2026-09-10-finmem-deep-dive.md` (361 行) — **L1/L2/L3 记忆细节** / observation-think-decision 循环
- `2026-09-10-openbb-deep-dive.md` (256 行) — **Provider ABC + 5 surface + MCP first-class** / **AGPL 警告**
- `2026-09-10-qlib-deep-dive.md` (457 行) — **Alpha158 配置驱动** / Exchange 回测参数 / qrun YAML workflow

---

## 8. 下一步行动

1. **跟用户拍板 O5-O10 决策**(本对比文档已给建议)
2. 更新 `2026-09-10-finance-general-agent-design.md`(按 §5 列的 8 个更新点)
3. 切分支 `codex/finance-general-agent`
4. 开始 Day 1:memory + DB + tools_bridge(MCP 重点)
5. 端到端:用户输入"招商银行最近的走势" → agent 调 quote/history/fundamentals/alpha158 → SSE 流式返回 + chart
