# 2026-09-10 — Finance General Agent Design

**Date:** 2026-09-10
**Stage:** C (Natural-Language General Agent)
**Status:** Draft — pending user review
**Branch:** (TBD — 当前在 `codex/quant-research-stage-b`,完成后切新分支 `codex/finance-general-agent`)
**前置依赖:** Stage B(本 PR 中)至少完成 B1(Alpha158),否则通用 agent 没有量化维度可调用

## Background

Today, every analyst function is started by an explicit button click or a
cron job:

- Watchlist 资产 → 手动点"开始分析"
- 定时任务 → 在 `/scheduled` 配置 cron
- 量化筛选 → 没有工具,要用户自己跑 LangChain tool

This forces the user to:
1. Know which tool to invoke (`compute_alpha_factors` vs `evaluate_alpha`?)
2. Know which params to pass (lookback_days, forward_days, ...)
3. Know which LangGraph analyst to trust

We want a **single natural-language entrypoint** — "帮我看看 600036 的仓位
风险,顺便跟 600000 做个对比" — that:

1. Parses intent into a **routing decision** (which sub-workflow / which analyst)
2. Grounds the request with **data acquisition** (行情、因子、新闻)
3. Runs the right **test / analysis pipeline** (基本面、技术、辩论)
4. **Delivers** a structured response back to the user (text + 引用数据)

This is the "理财通用 agent" — replaces the need to know the tool layer.

借鉴来源:
- [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 的 "Research Loop" 概念
  (Route → Ground → Test → Deliver)
- [A_Share_investment_Agent](https://github.com/24mlight/A_Share_investment_Agent)
  的角色化辩论
- LangGraph `langgraph.prebuilt.create_react_agent` + MCP `fastmcp` 框架

## Goal

用户用自然语言对话完成所有"理财决策辅助"任务,无需选择工具/参数:

1. **Single chat entrypoint** —— `/agent` 全屏对话或右侧抽屉
2. **Routing intelligence** —— "想看仓位风险" → 路由到 risk 模块;"想做量化筛选" → 路由到 quant 模块
3. **MCP server** —— 把 12+ LangChain tools 暴露成 MCP,Claude Desktop / Claude Code / 外部 agent 可直接调用
4. **Cross-session memory** —— 用户偏好、关注清单、历史对话跨 session 带过来

## Non-Goals

明确划出范围:

| Out | Reason |
|-----|--------|
| 自动执行真实交易(下单) | 永远只做决策辅助,不接券商 |
| 多用户/多租户 | 单用户假设 |
| 长期 RAG(基于历史报告的向量检索) | 等 Stage C 完成后再评估 |
| 自动写 LangGraph node | agent 只"调用"现有 node,不"生成"新 node |
| 多模态(图、语音) | 文本优先 |

## User Stories

1. As a user, I type "帮我看下 600036 现在能不能加仓" → agent 调
   fundamentals analyst + market analyst + 量化因子,给我一个综合建议。
2. As a user, I type "最近 30 天哪些 ETF 动量最强" → agent 跑批量
   `compute_alpha_factors`,按 `roc_20` 排序,返回 top 10。
3. As a user, I open Claude Desktop,加载我们的 MCP server,在 Claude 里
   问 "600036 的 RSI 怎么样",Claude 直接调 `compute_alpha_factors`。
4. As a user, 我上次说 "我只买银行股和科技股",这次直接说"找几只便宜的科技股",agent 从长期 memory 里读到偏好。
5. As a user, 我在 `/agent` 对话框说"给 600031 设个 8% 涨幅提醒" → agent 调
   alert API 建告警,告诉我建好了。

## Functional Scope

### In Scope (Stage C MVP)

| Item | Detail |
|------|--------|
| **Orchestrator** | LangGraph `create_react_agent`,基于现有 12 个 tool + alpha158 tool |
| **Routing** | 关键词 + LLM 自路由:查询意图 → 路由到 1+ 个 tool 调用 |
| **UI 形态** | **右侧抽屉**(常驻,可关闭),不在 `/analysis` 主视图 |
| **MCP server** | 用 `fastmcp` 把 3 个 alpha158 tools + 5 个核心 tools 暴露成 MCP |
| **Memory** | LangGraph `MemorySaver` + SQLite 持久化;支持用户偏好 / 历史对话 |
| **Action 权限** | **读 + 写** (可创建告警/笔记/调度任务,**不可触发分析跑全 pipeline**) |
| **触发方式** | Web UI + MCP 协议,**不**开放 HTTP webhook |

### Out of Scope (Stage C.1+ 后续)

| Item | Reason |
|------|--------|
| 自动触发 TradingAgents 全 pipeline | 风险高,需要审批流 |
| 语音输入 | 多模态延后 |
| 多用户隔离 | 单用户假设 |
| 自动写 LangGraph node | LLM 创造力不可控 |

## Architecture

### 模块划分

```
tradingagents/
├── agents/
│   └── general/                       [NEW]
│       ├── __init__.py
│       ├── orchestrator.py            ReAct agent + routing logic
│       ├── memory.py                  cross-session memory store
│       ├── prompts.py                 system prompt + 用户偏好模板
│       └── tools_bridge.py            把现有 12 tool 装到 agent
├── dataflows/
│   └── (已有 — 复用)
└── (B1 模块)

mcp_server/
├── __init__.py
├── server.py                          [NEW] fastmcp server,包装 alpha158 + 核心 tool
└── README.md

web/static/
├── agent.js                           [NEW] 抽屉前端
├── agent.css
web/
├── app.py                             [MOD] 加 /agent 路由 + SSE 事件流
└── routes/
    └── agent.py                       [NEW] /api/agent/chat, /api/agent/history

tests/
├── test_general_orchestrator.py      [NEW] routing logic 测试
├── test_mcp_server.py                 [NEW] MCP tools 调用测试
└── test_agent_memory.py               [NEW] cross-session 测试

docs/superpowers/specs/
└── 2026-09-10-finance-general-agent-design.md (THIS)
```

### 数据流

```
User: "帮我看 600036 现在能不能加仓"
    ↓
[Web UI 抽屉] → POST /api/agent/chat
    ↓
Orchestrator (ReAct Agent)
    ↓ parse intent
LLM 路由决策:
  - "调 fundamentals_analyst"
  - "调 compute_alpha_factors 600036.SS rsi_14,macd_hist,obv"
  - "调 get_news 600036.SS today"
    ↓
Tool 调用链(可能 5-10 步)
    ↓
中间事件流(SSE: tool_call_start / tool_call_done / text_delta)
    ↓
LLM 综合(text-davinci style final answer)
    ↓
SSE 流推送 → 抽屉 UI 增量渲染
    ↓
Memory 写入(SQLite): 用户偏好 + 本次对话 + 引用工具
```

### Routing 设计

两种策略,**LLM 自路由为主,关键词加速为辅**:

```python
# keywords → 直接路由(省一次 LLM 调用)
FAST_ROUTES = {
    r"加仓|减仓|卖出|买入|调仓": ["fundamentals_analyst", "risk_analyst"],
    r"涨跌|动量|波动|技术指标": ["market_analyst", "compute_alpha_factors"],
    r"新闻|公告|消息": ["news_analyst", "get_news"],
    r"筛选|排行|哪些标的": ["screen_factors"],
    r"提醒|告警|触发": ["create_alert"],
    r"笔记|记录|备忘": ["create_note"],
}

# 默认: LLM 自己 ReAct 路由(慢但灵活)
def route(user_query: str) -> list[str]:
    for pattern, tools in FAST_ROUTES.items():
        if re.search(pattern, user_query):
            return tools
    return None  # fallback to LLM
```

### Memory 设计

三层结构:

| 层 | 存储 | 内容 | 保留 |
|----|------|------|------|
| **L1 会话内** | LangGraph `MemorySaver` (in-memory) | 当前对话 turn | session 内 |
| **L2 跨会话偏好** | SQLite `user_preferences` | 风险偏好、关注行业 | 永久 |
| **L3 历史引用** | SQLite `agent_references` | 历史上调过哪些 tool/结果 | 永久,可检索 |

```sql
CREATE TABLE user_preferences (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
-- examples: "行业=银行,消费", "风险偏好=稳健", "持仓周期=中长线"

CREATE TABLE agent_references (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    timestamp TEXT,
    tool_name TEXT,
    tool_args TEXT,  -- JSON
    tool_result_hash TEXT,  -- 内容指纹
    user_query TEXT,
    summary TEXT
);
CREATE INDEX idx_agent_refs_session ON agent_references(session_id);
CREATE INDEX idx_agent_refs_time ON agent_references(timestamp);
```

### MCP Server 设计

```python
# mcp_server/server.py
from fastmcp import FastMCP, tool

mcp = FastMCP("tradingagents")

@mcp.tool()
def alpha_compute(symbol: str, factors: str, curr_date: str) -> str:
    """计算 alpha158 因子值"""
    return compute_alpha_factors.invoke({"symbol": symbol, "factors": factors, "curr_date": curr_date})

@mcp.tool()
def alpha_evaluate(symbol: str, factor: str, curr_date: str) -> str:
    """评估因子 IC/IR"""
    ...

@mcp.tool()
def quote(symbol: str) -> str:
    """获取最新行情"""
    ...

# 总共 8-10 个 MCP tools(精选高频)

if __name__ == "__main__":
    mcp.run()  # stdio transport
```

启动:`python -m mcp_server.server`,Claude Desktop 通过 `~/.config/claude/config.json` 配置 stdio MCP client。

## Interface Contract

### API 1: `POST /api/agent/chat`

```python
Request:
{
    "session_id": "uuid",
    "query": "帮我看 600036 现在能不能加仓",
    "stream": true  # SSE 流式
}

Response (SSE):
event: tool_call_start
data: {"tool": "compute_alpha_factors", "args": {...}}

event: tool_call_done
data: {"tool": "compute_alpha_factors", "duration_ms": 234, "result_preview": "..."}

event: text_delta
data: {"delta": "综合判断:"}

event: text_delta
data: {"delta": "基于当前 RSI 67.2,处于..."}

event: done
data: {"session_id": "...", "references_stored": 5}
```

### API 2: `GET /api/agent/history?session_id=xxx&limit=50`

返回历史 turns(读 L1 memory):

```json
{
    "session_id": "...",
    "turns": [
        {"role": "user", "content": "...", "timestamp": "..."},
        {"role": "assistant", "content": "...", "tool_calls": [...]}
    ]
}
```

### API 3: `GET /api/agent/preferences`

返回 L2 user preferences(读 SQLite):

```json
{
    "行业": ["银行", "消费"],
    "风险偏好": "稳健",
    "持仓周期": "中长线"
}
```

### API 4: `POST /api/agent/preferences`

更新 L2:

```json
{
    "key": "行业",
    "value": ["银行", "消费", "科技"]
}
```

## UI 设计

### 抽屉组件 (`web/static/agent.js`)

```html
<!-- 右侧抽屉,常驻,可关闭 -->
<div id="agent-drawer" class="drawer collapsed">
    <button class="drawer-toggle">💬</button>
    <div class="drawer-content">
        <div class="drawer-header">
            <h3>通用理财助手</h3>
            <button class="settings">⚙</button>
            <button class="clear">清空</button>
        </div>
        <div class="message-list" id="agent-messages">
            <!-- 消息流(用户/助手/工具调用卡片) -->
        </div>
        <div class="input-area">
            <textarea placeholder="问问理财相关的问题..."></textarea>
            <button>发送</button>
        </div>
        <div class="preferences-bar">
            <span>行业: 银行 · 消费</span>
            <span>风险: 稳健</span>
        </div>
    </div>
</div>
```

### 消息渲染

- **用户消息**:右对齐气泡
- **助手消息**:左对齐气泡 + Markdown 渲染
- **工具调用**:折叠卡片 `<details>` 默认展开,显示 `tool_name / args / result_preview`
- **SSE 流式**:打字机效果(`text_delta` 增量拼接到最后一条 assistant 消息)

### 视觉规范

- 复用现有 token 系统(`--accent`, `--bg-elevated`, etc.)
- dark mode 自动适配
- 抽屉宽度:420px(展开),48px 图标(收起)
- 拖拽改变宽度(可选,v2)

## Testing Plan

### 单元测试

| Test | 内容 |
|------|------|
| `test_routing_fast` | 关键词正则匹配正确 |
| `test_routing_llm_fallback` | LLM 路由 fallback 正常 |
| `test_memory_l1` | 会话内 MemorySaver 正常存取 |
| `test_memory_l2` | SQLite 偏好读写 |
| `test_memory_l3` | 历史引用写入+检索 |
| `test_mcp_tool_alpha_compute` | MCP 调用 alpha158 tool 成功 |
| `test_mcp_tool_quote` | MCP 调用 quote 成功 |

### 集成测试

| Test | 内容 |
|------|------|
| `test_e2e_simple_query` | "600036 RSI 多少" → 调 compute_alpha_factors → 返回答案 |
| `test_e2e_multi_turn` | "加仓吗?" → "跟 600000 比呢" → 多 turn 带上下文 |
| `test_e2e_memory_recall` | session 1 设偏好,session 2 自动带 |
| `test_e2e_claude_desktop` | 启动 MCP server,模拟 Claude Desktop stdio 调用 |

## Migration / Rollout

无需 DB 迁移(新加表,在 `web/migrations/011.sql` 跟 B2 一起做)。

部署:
1. merge `codex/finance-general-agent` → main
2. 打 tag `v0.7.0`(general agent)
3. 服务无需重启(模块加载)
4. README 加 MCP server 配置说明
5. 可选: 博客推文示例用法

## Risk & Open Questions

| # | Risk / Question | Mitigation / 待用户拍板 |
|---|-----------------|--------------------------|
| R1 | LLM 路由决策错误(调错 tool) | fast-route 用关键词过滤掉 80%,只 20% 走 LLM |
| R2 | 用户表达模糊("我想搞点股票") | LLM 主动追问 1-2 轮再执行 |
| R3 | Tool 调太多次很慢(>30s) | 设置 max_tool_calls=8,超时 30s |
| R4 | 跨会话 memory 泄露隐私 | L2/L3 默认关闭;用户在 settings 显式开启 |
| O5 | **UI 形态**:右侧抽屉 / 全屏页 `/agent` / 浮窗按钮? | **用户拍板**(下面选) |
| O6 | **Action 权限**:只读 / 读+写(建告警/笔记)/ 读+写+触发分析 | **用户拍板**(下面选) |
| O7 | **MCP 范围**:只暴露 3 个 alpha tool / 暴露所有 12 个 core tool / 暴露 + 写工具 | **用户拍板**(下面选) |
| O8 | **Memory 默认启用**:开 / 关 | **用户拍板**(下面选) |

## Success Criteria

- [ ] 用户用自然语言完成 5 类核心任务(看行情 / 因子 / 建告警 / 建笔记 / 跨标的对比)
- [ ] MCP server 启动后,Claude Desktop 可调用 `alpha_compute` 拿到结果
- [ ] 跨 session 偏好被正确读出(行业/风险偏好)
- [ ] Tool 调用错误率 < 5%(人工 review 10 个 query)
- [ ] 平均响应延迟 < 10s(简单 query < 3s)
- [ ] 不破坏现有 12 个 analyst 的 tool 调用
- [ ] merge 后打 `v0.7.0` tag

## 待用户拍板的 4 个关键决策

| # | 决策 | 选项 | 我的建议 |
|---|------|------|----------|
| **O5** | UI 形态 | A 右侧抽屉 / B 全屏页 `/agent` / C 浮窗按钮 | **A**(集成度高,跟现有 SPA 一致) |
| **O6** | Action 权限 | 1 只读 / 2 读+写(告警+笔记) / 3 读+写+触发分析 | **2**(平衡体验与风险) |
| **O7** | MCP 范围 | 1 只 3 alpha / 2 alpha + 5 core quote / 3 全 12 + 写工具 | **2**(核心能力,安全可控) |
| **O8** | Memory 默认启用 | on / off | **off 默认**,settings 里显式开启(R4 隐私) |

## File Manifest

| File | Status |
|------|--------|
| `tradingagents/agents/general/{orchestrator,memory,prompts,tools_bridge}.py` | 📝 新增 |
| `mcp_server/server.py` | 📝 新增 |
| `web/static/agent.{js,css}` | 📝 新增 |
| `web/app.py` | 📝 修改(加路由) |
| `web/routes/agent.py` | 📝 新增 |
| `tests/test_{general_orchestrator,mcp_server,agent_memory}.py` | 📝 新增 |
| `docs/superpowers/specs/2026-09-10-finance-general-agent-design.md` | ✅ 本文件 |

## Next Steps

1. 用户 review 本文档 + O5/O6/O7/O8 拍板
2. B1 完成后切 `codex/finance-general-agent` 分支
3. 实现 + 测试
4. merge main + 打 `v0.7.0` tag
