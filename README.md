<p align="center">
  <img src="assets/TauricResearch.png" style="width: 60%; height: auto;">
</p>

<div align="center" style="line-height: 1;">
  <a href="https://arxiv.org/abs/2412.20138" target="_blank"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2412.20138-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK" target="_blank"><img alt="Discord" src="https://img.shields.io/badge/Discord-TradingResearch-7289da?logo=discord&logoColor=white&color=7289da"/></a>
  <a href="https://x.com/TauricResearch" target="_blank"><img alt="X Follow" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&logoColor=white"/></a>
  <a href="https://github.com/TauricResearch/" target="_blank"><img alt="Community" src="https://img.shields.io/badge/GitHub_Community-TauricResearch-14C290?logo=discourse"/></a>
</div>
<br>
<div align="center">
  <a href="https://github.com/TauricResearch" target="_blank"><img alt="TradingAgents #1 Repository of the Day" src="https://trendshift.io/api/badge/repositories/16192" width="250" height="55"/></a>
</div>
<br>
<div align="center">
  <!-- Keep these links. Translations will automatically update with the README. -->
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=de">Deutsch</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=es">Español</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=fr">français</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ja">日本語</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ko">한국어</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=pt">Português</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ru">Русский</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=zh">中文</a>
</div>

---

# TradingAgentsPlus：多智能体资产分析与个人投资者平台

TradingAgentsPlus 基于 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 多智能体研究框架，增加了面向个人投资者的本地 Web 工作台。它把关注列表、行情快照、可配置分析任务和历史报告放在同一个界面中，帮助用户研究资产，不连接券商，也不会自动下单。

核心分析引擎仍然是 TradingAgents；本仓库的 Web 平台、数据适配、报告归档和 SQLite 持久化属于 TradingAgentsPlus 扩展。

## News
- [2026-07] **TradingAgents v0.3.1** released with correctness and stability fixes: Alpha Vantage look-ahead filtering, graph-router crash-safety, graph-shape-aware checkpoint resume, working crypto sentiment sources, a configurable LLM retry budget, Bedrock API-key auth, and Claude Sonnet 5 / Fable 5 support. See [CHANGELOG.md](CHANGELOG.md) for the full list.
- [2026-06] **TradingAgents v0.3.0** released with a verified data-access contract, an expanded provider registry (NVIDIA, Kimi, Groq, Mistral, Bedrock, and any OpenAI-compatible endpoint), FRED and Polymarket data vendors, a current-generation model catalog, and a CI gate.
- [2026-05] **TradingAgents v0.2.5** released with the grounded Sentiment Analyst, GPT-5.5 etc. model coverage, Qwen/GLM/MiniMax dual-region support, `TRADINGAGENTS_*` env-var configurability with API-key auto-detection, remote Ollama support, non-US alpha benchmarks, and ticker path-traversal hardening.
- [2026-04] **TradingAgents v0.2.4** released with structured-output agents (Research Manager, Trader, Portfolio Manager), LangGraph checkpoint resume, persistent decision log, DeepSeek/Qwen/GLM/Azure provider support, Docker, and a Windows UTF-8 encoding fix.
- [2026-03] **TradingAgents v0.2.3** released with multi-language support, GPT-5.4 family models, unified model catalog, backtesting date fidelity, and proxy support.
- [2026-03] **TradingAgents v0.2.2** released with GPT-5.4/Gemini 3.1/Claude 4.6 model coverage, five-tier rating scale, OpenAI Responses API, Anthropic effort control, and cross-platform stability.
- [2026-02] **TradingAgents v0.2.0** released with multi-provider LLM support (GPT-5.x, Gemini 3.x, Claude 4.x, Grok 4.x) and improved system architecture.
- [2026-01] **Trading-R1** [Technical Report](https://arxiv.org/abs/2509.11420) released, with [Terminal](https://github.com/TauricResearch/Trading-R1) expected to land soon.

<div align="center">

🚀 [TradingAgentsPlus](#tradingagents-framework) | ⚡ [安装与启动](#installation-and-cli) | 🌐 [本地 Web 工作台](#local-web-console) | 📦 [Package Usage](#tradingagents-package) | 🤝 [Contributing](#contributing) | 📄 [Citation](#citation)

</div>

> 🎉 **TradingAgents** officially released! We have received numerous inquiries about the work, and we would like to express our thanks for the enthusiasm in our community.
>
> So we decided to fully open-source the framework. Looking forward to building impactful projects with you!

## TradingAgents Framework

TradingAgents is a multi-agent trading framework that mirrors the dynamics of real-world trading firms. By deploying specialized LLM-powered agents: from fundamental analysts, sentiment experts, and technical analysts, to trader, risk management team, the platform collaboratively evaluates market conditions and informs trading decisions. Moreover, these agents engage in dynamic discussions to pinpoint the optimal strategy.

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

> TradingAgents framework is designed for research purposes. Trading performance may vary based on many factors, including the chosen backbone language models, model temperature, trading periods, the quality of data, and other non-deterministic factors. [It is not intended as financial, investment, or trading advice.](https://tauric.ai/disclaimer/)

Our framework decomposes complex trading tasks into specialized roles.

### Analyst Team
- Fundamentals Analyst: Evaluates company financials and performance metrics, identifying intrinsic values and potential red flags.
- Sentiment Analyst: Aggregates news headlines, StockTwits, and Reddit chatter into a single sentiment read to gauge short-term market mood.
- News Analyst: Monitors global news and macroeconomic indicators, interpreting the impact of events on market conditions.
- Technical Analyst: Utilizes technical indicators (like MACD and RSI) to detect trading patterns and forecast price movements.

<p align="center">
  <img src="assets/analyst.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Researcher Team
- Comprises both bullish and bearish researchers who critically assess the insights provided by the Analyst Team. Through structured debates, they balance potential gains against inherent risks.

<p align="center">
  <img src="assets/researcher.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Trader Agent
- Composes reports from the analysts and researchers to make informed trading decisions, determining the timing and magnitude of trades.

<p align="center">
  <img src="assets/trader.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Risk Management and Portfolio Manager
- Continuously evaluates portfolio risk by assessing market volatility, liquidity, and other risk factors. The risk management team evaluates and adjusts trading strategies, providing assessment reports to the Portfolio Manager for final decision.
- The Portfolio Manager approves/rejects the transaction proposal. If approved, the order will be sent to the simulated exchange and executed.

<p align="center">
  <img src="assets/risk.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

## Installation and CLI

### Installation

克隆 TradingAgentsPlus：
```bash
git clone git@github.com:jasonawe/TradingAgentsPlus.git
cd TradingAgentsPlus
```

如果本机尚未配置 GitHub SSH，也可以使用 HTTPS：

```bash
git clone https://github.com/jasonawe/TradingAgentsPlus.git
cd TradingAgentsPlus
```

Create a virtual environment in any of your favorite environment managers:
```bash
conda create -n tradingagents python=3.12
conda activate tradingagents
```

Install the package and its dependencies:
```bash
pip install .
```

### Docker

Alternatively, run with Docker:
```bash
cp .env.example .env  # add your API keys
docker compose run --rm tradingagents
```

For local models with Ollama:
```bash
docker compose --profile ollama run --rm tradingagents-ollama
```

### Required APIs

TradingAgents supports multiple LLM providers. Set the API key for your chosen provider:

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export DEEPSEEK_API_KEY=...        # DeepSeek
export DASHSCOPE_API_KEY=...       # Qwen — International (dashscope-intl.aliyuncs.com)
export DASHSCOPE_CN_API_KEY=...    # Qwen — China (dashscope.aliyuncs.com)
export ZHIPU_API_KEY=...           # GLM via Z.AI (international)
export ZHIPU_CN_API_KEY=...        # GLM via BigModel (China, open.bigmodel.cn)
export MINIMAX_API_KEY=...         # MiniMax — Global (api.minimax.io)
export MINIMAX_CN_API_KEY=...      # MiniMax — China (api.minimaxi.com)
export OPENROUTER_API_KEY=...      # OpenRouter
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
```

For Azure OpenAI, copy `.env.enterprise.example` to `.env.enterprise` and fill in your credentials.

For AWS Bedrock, install the extra with `pip install ".[bedrock]"`, set `llm_provider: "bedrock"`, configure AWS credentials (environment variables, `~/.aws/credentials`, or an IAM role) and `AWS_DEFAULT_REGION`, and use a Bedrock model ID, e.g. `us.anthropic.claude-opus-4-8-v1:0`.

For local models, configure Ollama with `llm_provider: "ollama"`. The default endpoint is `http://localhost:11434/v1`; set `OLLAMA_BASE_URL` to point at a remote `ollama-serve`. Pull models with `ollama pull <name>`, and pick "Custom model ID" in the CLI for any model not listed by default.

For any other OpenAI-compatible server (vLLM, LM Studio, llama.cpp, or a custom relay), use `llm_provider: "openai_compatible"` and set the endpoint via `backend_url` (or `TRADINGAGENTS_LLM_BACKEND_URL`), e.g. `http://localhost:8000/v1` for vLLM or `http://localhost:1234/v1` for LM Studio. The model is whatever your server serves. No key is needed for local servers; set `OPENAI_COMPATIBLE_API_KEY` when the endpoint requires one.

Alternatively, copy `.env.example` to `.env` and fill in your keys:
```bash
cp .env.example .env
```

### CLI Usage

启动交互式 CLI：
```bash
tradingagents          # installed command
python -m cli.main     # alternative: run directly from source
```

直接运行 `python -m cli.main` 时，交互流程依次为：

1. 输入标的代码（ticker symbol）
2. 输入分析日期
3. 选择报告输出语言
4. 选择分析师团队
5. 选择分析深度
6. 选择模型厂商
7. 选择快速思考模型
8. 选择深度思考模型

CLI 只负责研究和生成分析报告，不执行真实交易。API 密钥和模型相关配置通过 `.env` 或 `TRADINGAGENTS_*` 环境变量提供。

### Local Web Console

TradingAgentsPlus 提供中文本地 Web 工作台，用于管理关注列表、查看只读行情快照、创建分析任务、跟踪运行状态和重新打开已完成报告。先安装项目，再启动本地服务：

```bash
pip install .
tradingagents web
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。如果 8000 端口已被占用，可以指定其他端口：

```bash
tradingagents web --port 8765
```

工作台使用浏览器历史记录支持多路由导航，刷新或点击浏览器后退不会退回到无关页面：

| 页面 | 路径 | 用途 |
| --- | --- | --- |
| 我的关注 | `/` | 添加关注资产，查看实时行情和最近一次分析摘要 |
| 分析任务 | `/analysis` | 创建新的资产分析，选择团队、深度、厂商、模型和语言 |
| 进行中 | `/active` | 查看当前运行中的分析任务和事件进度 |
| 定时任务 | `/scheduled` | 为关注资产配置 Cron、启停任务、立即运行并查看触发历史 |
| 报告库 | `/reports` | 筛选、打开和下载已完成的分析报告 |
| 报告详情 | `/reports/<report_id>` | 查看单份报告的综合结论和智能体明细 |
| 设置诊断 | `/settings` | 查看当前模型配置、行情供应商和数据状态 |

界面固定使用简体中文。每次分析可以单独选择报告语言，支持 CLI 的 11 种语言（`English`、`Chinese`、`Japanese`、`Korean`、`Hindi`、`Spanish`、`Portuguese`、`French`、`German`、`Arabic`、`Russian`）。报告正文按模型生成的原文展示，不会在浏览器端二次机器翻译。

服务默认绑定 `127.0.0.1`，适合本机使用。工作台不会向浏览器发送、保存或展示模型厂商 API 密钥；模型厂商、模型、报告语言、接口地址等配置继续从 `.env`、`TRADINGAGENTS_*` 环境变量和 `DEFAULT_CONFIG` 读取，请在启动前完成配置。

To open the console from another browser or device on the same network, bind it to all interfaces and use the host machine's LAN address:

```bash
tradingagents web --host 0.0.0.0 --port 8000
```

For example, open `http://192.168.112.100:8000` when `192.168.112.100` is the host machine's LAN address. Only do this on a trusted network because the console has no login layer.

关注列表行情是只读市场快照；平台不连接券商、不执行交易，也不管理真实持仓。当前默认使用 `yfinance`，配置 `ALPHA_VANTAGE_API_KEY` 后可在需要时回退到 Alpha Vantage。每条行情会记录来源、报价时间、抓取时间、缓存状态（`live`、`hit`、`miss`、`stale`）、数据新鲜度（`fresh`、`delayed`、`stale`、`unavailable`）和过期秒数。设置诊断页会展示每个实际尝试过的供应商健康状态、成功/失败次数、最近错误和延迟；前端请求使用 4 秒超时与 5/10/20/40/60 秒退避，并在页面不可见时暂停刷新。Polygon、Twelve Data、Tushare、AKShare 等供应商可以在同一 Provider 契约后扩展接入。

定时任务使用标准 5 字段 Cron 和服务所在时区。任务、触发日志、全局启用开关及最大并发数都持久化到 SQLite；服务重启后会恢复仍启用的任务，但不会补跑停机期间错过的触发。定时分析优先复用该资产最近一次成功分析的参数，没有历史时使用全局默认参数。删除关注资产会在同一事务中删除对应定时任务和历史日志。默认最多同时运行 3 个不同资产的分析，可在 `/scheduled` 的设置中调整为 1–10；同一资产不会并行启动第二个任务。

运行元数据、事件、关注列表、行情缓存、供应商健康状态和报告索引统一持久化到配置结果目录下的 SQLite（默认文件名为 `web_runs.sqlite3`，现有数据库启动时自动迁移）。实时事件通过内存 SSE 推送。关闭或刷新浏览器后可以重新连接正在运行的任务；如果 Web 服务本身重启，尚未完成的任务会标记为“已中断”，因为 SQLite 只能保存状态，不能恢复已经发出的模型请求。

任务默认最长运行 7200 秒，工作线程每 15 秒写入一次心跳，180 秒没有有效心跳会进入 `timed_out` 终态。可以用 `TRADINGAGENTS_RUN_TIMEOUT_SECONDS`、`TRADINGAGENTS_RUN_HEARTBEAT_INTERVAL_SECONDS` 和 `TRADINGAGENTS_RUN_HEARTBEAT_TIMEOUT_SECONDS` 调整；合法范围分别为 300-86400、5-60 和 30-600 秒。终态发布使用条件更新和最终状态校验，避免完成、取消和超时互相覆盖。

已完成报告仍保存在 `TRADINGAGENTS_RESULTS_DIR` 指定的目录（默认 `~/.tradingagents/logs/web_reports`），SQLite 只保存可筛选、可分页的报告索引和待重试 outbox。`GET /api/history` 默认返回兼容的旧列表；传入 `page`/`page_size` 后返回分页对象，支持标的、资产类型、状态、日期、关键词和排序筛选。索引缺失时会从磁盘报告重建，详情接口仍可兼容读取旧报告。报告级 `data_status` 与每个行情数据集的 `freshness` 分开记录。

To run the optional browser smoke tests locally, install the test extra and the Playwright browser once:

```bash
pip install -e ".[dev,web]"
python -m playwright install chromium
TRADINGAGENTS_PLAYWRIGHT=1 pytest -q tests/test_web_browser.py
```

### Markets and tickers

TradingAgents works with any market Yahoo Finance covers, using the exchange-suffixed ticker. Company identity and the alpha benchmark resolve automatically per market.

- US: `AAPL`, `SPY`
- Hong Kong: `0700.HK` · Tokyo: `7203.T` · London: `AZN.L`
- India: `RELIANCE.NS`, `.BO` · Canada: `.TO` · Australia: `.AX`
- China A-shares: Shanghai `.SS`, Shenzhen `.SZ` (e.g. `600519.SS` for Kweichow Moutai)
- Crypto: `BTC-USD`, `ETH-USD`

<p align="center">
  <img src="assets/cli/cli_init.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

An interface will appear showing results as they load, letting you track the agent's progress as it runs.

<p align="center">
  <img src="assets/cli/cli_news.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

<p align="center">
  <img src="assets/cli/cli_transaction.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

## TradingAgents Package

### Implementation Details

We built TradingAgents with LangGraph to ensure flexibility and modularity. The framework supports multiple LLM providers: OpenAI, Google, Anthropic, xAI, DeepSeek, Qwen (Alibaba DashScope, international and China endpoints), GLM (Zhipu), MiniMax (global + China), OpenRouter, Ollama for local models, and Azure OpenAI for enterprise.

### Python Usage

To use TradingAgents inside your code, you can import the `tradingagents` module and initialize a `TradingAgentsGraph()` object. The `.propagate()` function will return a decision. You can run `main.py`, here's also a quick example:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

You can also adjust the default configuration to set your own choice of LLMs, debate rounds, etc.

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # e.g. openai, google, anthropic, deepseek, groq, ollama; openai_compatible covers any OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp, ...)
config["deep_think_llm"] = "gpt-5.5"     # Model for complex reasoning
config["quick_think_llm"] = "gpt-5.4-mini" # Model for quick tasks
config["max_debate_rounds"] = 2

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

See `tradingagents/default_config.py` for all configuration options.

## Persistence and Recovery

TradingAgents persists two kinds of state across runs.

### Decision log

The decision log is always on. Each completed run appends its decision to `~/.tradingagents/memory/trading_memory.md`. On the next run for the same ticker, TradingAgents fetches the realised return (raw and alpha vs SPY), generates a one-paragraph reflection, and injects the most recent same-ticker decisions plus recent cross-ticker lessons into the Portfolio Manager prompt, so each analysis carries forward what worked and what didn't.

Override the path with `TRADINGAGENTS_MEMORY_LOG_PATH`.

### Checkpoint resume

Checkpoint resume is opt-in via `--checkpoint`. When enabled, LangGraph saves state after each node so a crashed or interrupted run resumes from the last successful step instead of starting over. On a resume run you will see `Resuming from step N for <TICKER> on <date>` in the logs; on a new run you will see `Starting fresh`. Checkpoints are cleared automatically on successful completion.

Per-ticker SQLite databases live at `~/.tradingagents/cache/checkpoints/<TICKER>.db` (override the base with `TRADINGAGENTS_CACHE_DIR`). Use `--clear-checkpoints` to reset all of them before a run.

```bash
tradingagents analyze --checkpoint           # enable for this run
tradingagents analyze --clear-checkpoints    # reset before running
```

```python
config = DEFAULT_CONFIG.copy()
config["checkpoint_enabled"] = True
ta = TradingAgentsGraph(config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
```

## Reproducibility

TradingAgents is LLM-driven, so two runs of the same ticker and date can differ. This is expected for a research tool built on language models, not a defect. The variation comes from a few distinct sources, and it helps to separate them.

Language model sampling is non-deterministic. Even at a fixed temperature, providers do not guarantee byte-identical output across calls, and reasoning models (the default GPT-5.x family, and any thinking-mode model) vary the most because their internal reasoning is itself sampled.

Live data moves. News, StockTwits, and Reddit return different content as time passes, so a run today sees different inputs than a run last week even for the same historical trade date. Pin the analysis date to hold the price and indicator window fixed, but the social and news sources still reflect "now".

To reduce variation you can lower the sampling temperature. Set `temperature` in your config (or `TRADINGAGENTS_TEMPERATURE` in `.env`); lower values make models that honor it more repeatable. The current curated models are reasoning-first and largely ignore temperature, so for tighter reproducibility use a non-reasoning model, which you can set explicitly via the Custom model ID option.

```python
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"
config["temperature"] = 0.0
# Reasoning models ignore temperature. For tighter reproducibility, set a
# non-reasoning deep/quick model explicitly (e.g. via the Custom model ID option).
```

What does not vary anymore: the analyzed company identity is resolved deterministically from the ticker before any agent runs, and the market analyst grounds exact price and indicator claims in a verified data snapshot. Earlier reports of "different companies" or fabricated price levels across runs are addressed by these two mechanisms.

Backtest results are not guaranteed to match any published figure. Returns depend on the model, the temperature, the date range, data quality, and the sampling above. Treat the framework as a research scaffold for studying multi-agent analysis, not as a strategy with a fixed, replicable return.

## Contributing

Contributions are welcome: bug fixes, documentation, and feature ideas; past contributions are credited per release in [`CHANGELOG.md`](CHANGELOG.md).

## Citation

Please reference our work if you find *TradingAgents* provides you with some help :)

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```
