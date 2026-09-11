# TradingAgents MCP Server

把 TradingAgentsPlus 的 21 个理财工具(15 原有 + 6 Day 7)暴露为 [MCP (Model Context Protocol)](https://modelcontextprotocol.io) 端点,让 Claude Desktop / Cursor / Claude Code 等 MCP 客户端能直接调用。

## 启动

### 1. 准备环境

```bash
pip install 'mcp<2'           # v1.x API,稳定
cp .env.example .env
# 编辑 .env,填入 MINIMAX_CN_API_KEY=sk-...
```

### 2. 启动 MCP server(stdio transport)

```bash
cd /path/to/TradingAgents
python -m tradingagents.agents.general.mcp_server
```

stdio transport — server 通过 stdin/stdout 与 MCP 客户端通信。

### 3. 客户端配置

#### Claude Desktop

macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
Linux: `~/.config/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "tradingagents-agent": {
      "command": "python",
      "args": ["-m", "tradingagents.agents.general.mcp_server"],
      "cwd": "/path/to/TradingAgents",
      "env": {
        "MINIMAX_CN_API_KEY": "sk-cp-YOUR_KEY_HERE",
        "TRADINGAGENTS_LLM_PROVIDER": "minimax-cn"
      }
    }
  }
}
```

重启 Claude Desktop,工具列表里会显示 21 个 TradingAgents 工具。

#### Cursor(`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "tradingagents-agent": {
      "command": "python",
      "args": ["-m", "tradingagents.agents.general.mcp_server"],
      "cwd": "/path/to/TradingAgents",
      "env": {
        "MINIMAX_CN_API_KEY": "sk-cp-YOUR_KEY_HERE",
        "TRADINGAGENTS_LLM_PROVIDER": "minimax-cn"
      }
    }
  }
}
```

## 21 个可用工具

### 读工具(8)

| Tool | Description |
|------|-------------|
| `get_quote` | 单个 symbol 实时报价(price / change / volume / source) |
| `get_quotes_batch` | 批量查询(最多 20 个),返回 markdown 表格 |
| `get_history` | K 线历史 + 技术指标摘要(MA20/RSI14) |
| `get_fundamentals` | 基本面(PE/PB/市值/ROE/营收) |
| `list_watchlist` | 我的关注列表 |
| `list_alpha_factors` | 列出 158 个 alpha 因子(分动量/波动/量价/趋势) |
| `compute_alpha_factors` | 计算指定 symbol 的因子值 |
| `evaluate_alpha` | IC/IR 评估因子预测能力 |

### 分析工具(3)— Day 7

| Tool | Description |
|------|-------------|
| `run_trading_agents_analysis` | 启动主图跑完整 pipeline,返回 run_id |
| `get_analysis_status` | 查 run 状态(pending/running/completed/failed) |
| `list_reports` | 列出历史分析报告 |

### 数据/调度(3)— Day 7

| Tool | Description |
|------|-------------|
| `get_news` | 拿某资产的最近 N 天新闻(含 sentiment) |
| `list_scheduled_tasks` | 列出所有定时分析任务 |
| `run_scheduled_task` | 立即触发一个定时任务 |

### 写工具(7)— HITL 写操作(默认 auto-approve)

| Tool | Description |
|------|-------------|
| `create_note` / `update_note` / `delete_note` | 笔记 CRUD |
| `create_alert` / `update_alert` / `delete_alert` | 告警 CRUD |
| `update_preference` | 更新用户偏好(L2) |

## 关键设计

### HITL 模式

- **默认 (auto-approve)**: MCP 客户端调用写工具时,**直接执行**(MCP 上下文默认信任 Claude agent)。
- **严格模式**: 设 `MCP_REQUIRE_CONFIRM=1` env var,写工具返回 `AWAITING_CONFIRMATION: {...}` 标记,需要调用方手动确认。

```bash
MCP_REQUIRE_CONFIRM=1 python -m tradingagents.agents.general.mcp_server
```

### session_id 自动注入

写工具(`create_*` / `delete_*` / `update_*`)需要 `session_id`(用于审计 + L3 引用)。MCP 客户端不需要手动传 — server 自动生成 `mcp_<uuid16>` 作为 `config.configurable.thread_id`。

### 基础设施复用

MCP server 完全复用 web app 的基础设施:
- SQLiteStore(`~/.tradingagents/web_runs.sqlite3`)
- NoteRepository / AlertRepository
- ProviderRouter(4 个 provider:yfinance / alpha_vantage / eastmoney / akshare)
- QuoteService

不重复实现任何 CRUD / 数据访问逻辑。

## 故障排查

| 问题 | 修复 |
|------|------|
| `ModuleNotFoundError: No module named 'mcp'` | `pip install 'mcp<2'` |
| `validation error for handlerArguments` | 确认装的是 `mcp<2`(v1.x),v2 API 不兼容 |
| `RunManager 未注入` | 检查 stderr — 基础设施 import 失败 |
| Tool 返回 `No data` | 换 provider / 改 symbol |
| 写工具 audit log 失败 | 检查 `~/.tradingagents/web_runs.sqlite3` 权限 |

## 测试

```bash
python tests/test_d5_mcp.py
# 输出:
# [1] initialize + tools/list: 21 tools
# [2] get_quote(600036.SS) → 真实行情
# [3] create_note(600036.SS, ...) → NOTE_CREATED + uuid
```

## 设计文档

完整设计:`docs/superpowers/specs/2026-09-10-finance-general-agent-design.md`
