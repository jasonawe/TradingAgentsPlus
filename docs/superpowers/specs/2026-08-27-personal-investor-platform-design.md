# TradingAgents 个人投资者研究平台设计规格

**日期：** 2026-08-27

**状态：** 已确认方向，修订后待规格复审

## 1. 产品定位

TradingAgents Web 定位为面向个人投资者的资产研究平台：

1. 通过关注列表持续查看用户关心的资产行情。
2. 从关注列表或分析入口发起一次资产分析。
3. 使用 TradingAgents 多智能体生成结论优先的资产报告。
4. 保存报告及其数据来源，支持之后复查当时的分析依据。

产品不执行交易，不连接券商，不同步真实账户，也不维护真实持仓、成本、盈亏或订单状态。代码中的“交易员”“风险管理”“投资组合经理”是内部分析角色，产品层统一显示为研究流程和分析结论。

## 2. 用户与核心任务

目标用户是希望集中跟踪和研究股票、ETF、加密货币等资产的个人投资者。核心任务是：

- 添加、排序、删除关注资产；
- 快速查看最新可用价格、涨跌幅、市场状态和更新时间；
- 了解某个资产是否已经有近期报告；
- 用指定日期、研究团队、研究深度和模型发起分析；
- 先看结论，再按需查看证据、数据来源和各智能体报告；
- 在行情暂时不可用时看到明确的缓存或数据过期状态，而不是错误的实时假象。

## 3. 范围

### 本期包含（MVP）

- 中文固定界面的个人研究首页；
- 关注列表和资产行卡片；
- 当前行情快照、日内/历史走势图数据接口；
- 资产详情页，串联行情、近期报告和“开始分析”；
- 分析任务页，展示任务状态、可离开后重新进入；
- 报告库和报告详情页；
- 报告的结论摘要、评级、分析参数、数据来源和数据时间；
- 模型厂商、快速思考模型、深度思考模型配置和选择；
- 报告输出语言选择，默认中文；
- 现有 `yfinance` 和 `Alpha Vantage` 的行情适配；
- 行情供应商优先级、缓存时长和过期策略的服务端配置；
- 一个默认的“我的关注”列表；
- SQLite 本地存储和清晰的仓储接口。

Polygon、Twelve Data、Tushare、AKShare 在本规格中是后续可插拔适配器，不在 MVP 中宣称已经可用；只有实现适配器、依赖和凭据检查后，才可以出现在供应商目录中。

### 本期不包含

- 交易下单、模拟撮合或券商集成；
- 持仓、资金、成本、收益、资产配置和组合再平衡；
- 价格提醒、推送通知和自动交易信号执行；
- 在报告生成后用新行情静默改写报告；
- 将第三方行情接口的密钥放进浏览器；
- 把任意 Markdown 当 HTML 执行。

### 后续阶段

多关注列表、WebSocket 行情、Redis、PostgreSQL、对象存储、可编辑的非密钥设置和新增供应商适配器属于后续阶段。MVP 先稳定数据契约和失败语义，再扩展吞吐和供应商覆盖。

## 4. 信息架构与交互

界面所有可见文案固定使用简体中文，不提供中英文界面切换。分析报告的输出语言是独立设置，支持现有语言列表，默认“中文”；用户可以让报告输出为英语、日语等其他语言，但导航、按钮、状态和错误信息仍然是中文。

### 4.1 首页：我的关注

MVP 只有一个默认列表“我的关注”，首页只有一个主任务：查看和进入资产研究。多列表 API 先不开放，数据模型保留 `watchlist_id` 以便后续扩展。

- 顶部：产品名、添加资产、打开报告库、设置；
- 主区：关注列表；
- 每行：代码/名称、最新价、涨跌额、涨跌幅、市场状态、数据时间、数据源和数据状态；
- 行操作：查看资产、开始分析、移除关注；
- 空状态：直接提供“添加第一个资产”；
- 行情失败：显示最近缓存值和“数据过期/暂不可用”，并提供重试；
- 不展示账户余额、持仓数量、盈亏金额或买卖按钮。

关注列表的主操作是“添加资产”和“开始分析”，符合单一主操作与最小干预原则。删除关注需要二次确认，但不会删除历史报告。

### 4.2 资产详情

资产详情按从事实到行动的顺序排列：


1. 资产身份和当前行情快照；
2. 日内/历史价格图；
3. 最近分析报告和评级；
4. 数据来源与更新时间；
5. “开始分析”按钮。

行情变化不会自动刷新已经保存的报告。用户点击“开始分析”时，页面明确展示本次分析将使用的分析日期和数据供应商策略。ETF 在 MVP 中按 `stock` 资产类型处理；规范化代码后，`BTCUSD` 与 `BTC-USD` 等同一资产不能重复加入关注列表。

### 4.3 新建分析

保留 CLI 的八个概念步骤，但在 Web 中使用分段表单：

1. 资产代码；
2. 分析日期；
3. 报告输出语言；
4. 分析师团队；
5. 分析深度；
6. 模型厂商；
7. 快速思考大模型；
8. 深度思考大模型。

提供“快速分析”和“自定义分析”两个入口。快速分析使用默认团队、深度、模型和中文报告；自定义分析展开全部设置。模型选择只展示服务端目录中真实可用的组合，密钥和端点不进入请求。

### 4.4 分析任务

进行中的分析单独使用任务视图，不嵌入完成报告头部。任务视图显示：

- 当前阶段；
- 已完成/进行中/待处理的智能体；
- 当前进度和可读状态；
- 已抓取的数据类别；
- 取消分析；
- 离开页面后通过任务记录重新进入。

分析完成后，任务页只显示“查看报告”，报告页不显示“研究台进度”。

### 4.5 报告详情

报告采用结论优先布局：

- 资产、分析日期、评级和生成时间；
- 大模型综合摘要；
- 风险提示和关键依据；
- 数据来源、数据时间、延迟/过期标记；
- 分析配置：团队、深度、厂商、快速模型、深度模型、报告语言；
- 详细智能体报告默认折叠；
- 下载 Markdown。

Markdown 表格必须转换成真正的 HTML 表格，窄屏时允许表格区域横向滚动；报告内容只进行安全渲染和转义。

### 4.6 报告库

报告库按资产代码、分析日期、评级、生成时间、模型和数据状态筛选。列表展示报告摘要，不重新运行分析。历史报告即使行情供应商当前不可用也必须可以打开。

## 5. 行情和研究数据架构

关注列表行情与 TradingAgents 分析输入使用同一套供应商适配思想，但属于两个不同的服务入口。

```text
关注列表 -> QuoteService -> 行情供应商 -> 短期缓存 -> 页面行情

开始分析 -> AnalysisDataSnapshot -> 研究数据供应商
           -> TradingAgents 图 -> 不可变报告 + 数据快照清单
```

### 5.1 统一数据契约与行情字段

供应商适配器必须实现统一的 `QuoteProvider` 协议，并通过 `ProviderRouter` 接收明确的有序供应商链。只有链中列出的供应商可以尝试；未配置的供应商不得静默接管。错误分为 `not_configured`、`rate_limited`、`timeout`、`no_data`、`invalid_symbol` 和 `provider_error`。除 `invalid_symbol` 与 `no_data` 外，其余错误可按配置继续尝试下一项。

每个行情快照至少包含以下字段，时间统一使用带时区的 ISO-8601 UTC 字符串：

- 用户代码和规范化代码；
- 资产类型、交易所、币种；
- 最新价、前收、涨跌额、涨跌幅；
- 开高低、成交量（供应商支持时）；
- 市场是否开盘；
- 报价时间和抓取时间；
- 数据源；
- 是否实时、延迟或过期；
- 供应商原始响应摘要和错误码。

`freshness` 是固定枚举：`fresh`、`delayed`、`stale`、`unavailable`。超过配置 TTL 自动变为 `stale`；没有可显示价格时为 `unavailable`。批量请求返回逐资产结果，单个资产失败不影响其他资产，并在顶层返回 `partial: true`。原始响应只保留字段白名单和长度上限处理后的摘要，不保存密钥、请求头、完整 URL 或无限大小的供应商响应。

页面必须显示“截至时间”和“来源”，不允许只显示一个没有时间语义的“当前价格”。

### 5.2 供应商路由

供应商目录分两层：MVP 目录只包含已存在代码路径的 `yfinance` 和 `Alpha Vantage`；未来目录才加入 Polygon、Twelve Data、Tushare、AKShare。缺少 Python 依赖、API Key 或 Token 时，供应商状态为 `not_configured`，不会被 UI 标记为可用。

推荐路由方向：

| 市场/能力 | 主供应商 | 备用供应商 | 产品策略 |
|---|---|---|---|
| 美股关注列表行情 | MVP：yfinance | Alpha Vantage | 当前以最近可用行情为准；未来 Polygon/Twelve Data 适配器完成后才可标记实时或延迟 |
| 全球股票、外汇、加密货币 | MVP：yfinance | Alpha Vantage | 统一规范化代码，记录交易所和币种 |
| A 股、港股 | MVP：已覆盖范围内的 yfinance/Alpha Vantage | 未来 Tushare Pro/AKShare | 未实现适配器前显示“不支持或未配置”，不伪装为已支持 |
| 分析用 OHLCV/技术指标/基本面/新闻 | 现有 yfinance | Alpha Vantage | 保持现有分析供应商路由，分析开始时生成快照 |
| 宏观指标 | FRED | 无强制备用 | 缺失时报告明确标记不可用 |
| 预测市场 | Polymarket | 无强制备用 | 作为可选研究补充，不阻断核心分析 |

供应商路由由资产市场、数据能力和配置共同决定。不能因为主供应商失败就悄悄切换到用户没有配置的供应商；只有明确配置的备用链可以切换。供应商优先级采用服务端配置，不允许浏览器提交任意 URL 或凭据。

供应商能力和限制必须在设置页显示为中文说明。例如，Alpha Vantage 的报价接口支持全球代码，但普通模式主要是日终数据，实时或 15 分钟延迟美股数据需要相应权限；Polygon 的快照中报价/成交字段也取决于套餐；Twelve Data 同时提供 Quote 和 WebSocket 能力；Tushare 的 A 股日线有固定入库时间和积分限制。具体能力以供应商当前条款为准：

- [Polygon 单票快照](https://polygon.io/docs/rest/stocks/snapshots/single-ticker-snapshot?auth=signup)
- [Twelve Data Quote](https://twelvedata.com/docs/volume-indicators/ad-indicator)
- [Twelve Data WebSocket](https://support.twelvedata.com/en/articles/5620516-how-to-stream-the-data)
- [Alpha Vantage API 文档](https://www.alphavantage.co/documentation/)
- [Tushare 通用行情](https://tushare.pro/document/1?doc_id=109)
- [Tushare A 股日线](https://tushare.pro/document/2?doc_id=27)

### 5.3 缓存和过期

- 关注列表行情缓存默认 15 至 60 秒，按供应商和市场状态调整；
- 历史 K 线按资产、周期、日期和供应商缓存；
- 分析任务使用独立的数据快照，不直接读取会变化的页面缓存；
- 供应商失败时返回最近缓存，并显示过期时间；
- 没有成功数据时返回“暂无可用行情”，不估算、不填充、不让模型臆造价格；
- 同一资产和同一时间窗口的分析输入使用幂等缓存键，避免重复抓取。

## 6. 可扩展存储设计

SQLite 是本地单用户默认实现，但领域接口不直接依赖 SQLite。MVP 保留现有 `web_runs` 作为分析运行记录的物理表，`AnalysisRunRepository` 封装它；不另建一套并行的 `analysis_runs` 表。未来迁移时使用版本化 migration 将其迁移到 PostgreSQL。

### 6.1 领域实体

- `watchlists`：MVP 只有固定默认列表，保存名称、排序和创建时间；
- `watchlist_items`：代码、规范化代码、资产类型、交易所、排序、备注；
- `market_quotes`：行情快照、供应商、报价时间、抓取时间、延迟和过期状态；
- `market_candles`：历史/日内 K 线缓存；
- `web_runs`：现有分析请求、状态机、进度和错误，由 `AnalysisRunRepository` 封装；
- `analysis_data_snapshots`：一条分析任务一份 manifest，按数据集记录供应商、时间窗口、状态、内容引用和 SHA-256；
- `analysis_reports`：MVP 可由现有报告目录和 `run.json` 作为文件型 read model，表结构预留但不与 `web_runs` 重复写入；
- `settings`：非密钥的默认模型、供应商链、缓存策略和界面偏好。

报告正文和大体积原始响应不直接塞入主表：MVP 使用文件系统，数据库保存引用和校验哈希；对象存储部署属于后续阶段。

### 6.2 迁移边界

仓储接口至少提供：

- `WatchlistRepository`；
- `QuoteRepository`；
- `AnalysisRunRepository`；
- `ReportRepository`；
- `DataSnapshotRepository`。

第一阶段用 SQLite 实现；未来迁移 PostgreSQL 时不改变 API 和领域服务。MVP 的迁移版本从现有 `web_runs` 和报告目录读取，不删除旧数据。Redis 只用于后续短期行情缓存和任务协调，不作为报告唯一存储。

### 6.3 一致性和恢复

- 分析运行状态使用 `queued -> running -> publishing -> completed/failed/cancelled/interrupted` 状态机；`publishing` 是不可取消的中间态，所有终态都可由 HTTP 查询；
- MVP 任务事件继续使用现有内存缓冲和序号重放，重启后客户端以持久化运行记录为准；后续阶段再持久化 `analysis_events`；
- 服务重启后，`queued`/`running` 标记为 `interrupted`；`publishing` 根据提交标记恢复为 `completed`，否则清理临时目录并标记 `failed`；
- 完成采用两阶段发布：事务先将 `running` CAS 为 `publishing`，再把临时报告目录、manifest 和 `run.json` fsync 后 rename 到受控报告根目录并写入 `COMMITTED` 标记，最后事务将 `publishing` CAS 为 `completed`。取消只允许对 `queued`/`running` CAS，不能介入 `publishing`；重复调用幂等；
- 报告完成后不可变，重新分析生成新版本而不是覆盖旧报告；
- 删除关注资产不删除其报告、快照和分析任务记录；
- 所有供应商请求设置超时、重试、限流和结构化错误。

`interrupted` 是终态，HTTP `GET /api/runs/{id}` 返回 `status: "interrupted"`、`error_code: "service_restart"`；事件流使用独立的 `run_interrupted` 事件和 `RunInterruptedPayload`，旧客户端可将未知终态映射为失败展示。终态事件在 `web_run_events` SQLite 表持久化；若旧记录无事件，首次连接 `/events` 时按 `run_id` 合成一次 `run_interrupted`，写入后按事件序号去重。客户端可从该记录发起新的分析，但不能恢复原线程继续写报告。取消成功后清理临时目录，服务启动时清理所有未完成临时目录。报告目录只有同时存在 `COMMITTED`、完成状态 `run.json` 且数据库运行记录为 `completed` 时才被 history 索引；任一条件缺失都不展示。恢复任务按上述状态协议补偿或隔离，不把半提交目录当报告。

## 7. 设置和数据快照契约

配置加载顺序固定为：启动时读取 `DEFAULT_CONFIG`，再读取 SQLite `settings` 白名单键，最后应用环境变量；请求到达后，显式请求字段只覆盖本次运行。快速分析请求显式指定 `output_language=Chinese`，所以只有自定义分析省略该字段时才按环境变量 > SQLite > 默认配置解析。MVP 不提供浏览器写入 API Key、Token、端点；设置页只读展示供应商能力、配置状态、当前优先级链和缓存 TTL。SQLite settings 读取失败时记录诊断并退回 `DEFAULT_CONFIG`，不阻止服务启动；migration 失败则按上一节约定拒绝启动。未来如允许修改非密钥设置，必须使用白名单字段和版本化更新。

行情策略与 LLM 配置分开命名。MVP 服务端只提供 `default-yfinance` 和 `fallback-yfinance-alpha-vantage` 两个 `quote_strategy_id`，其中默认策略为 `["yfinance"]`，备用策略为 `["yfinance", "alpha_vantage"]`。`GET /api/settings` 返回策略目录和每项的供应商状态；`AnalysisRequest.quote_strategy_id` 可选，省略时按上述配置优先级解析。策略 ID 不允许自定义 URL、密钥或供应商名称列表。

`analysis_data_snapshots` 的最小结构为：

```json
{
  "id": "snapshot-...",
  "run_id": "run-...",
  "dataset": "ohlcv|technical|fundamentals|news|macro|sentiment|prediction_market",
  "symbol": "AAPL",
  "provider": "yfinance",
  "provider_chain": ["yfinance", "alpha_vantage"],
  "window_start": "2026-08-20T00:00:00Z",
  "window_end": "2026-08-27T00:00:00Z",
  "as_of": "2026-08-27T00:00:00Z",
  "fetched_at": "2026-08-27T20:00:03Z",
  "freshness": "fresh",
  "status": "complete|partial|unavailable",
  "payload_ref": "snapshots/run-.../datasets/ohlcv.json",
  "sha256": "sha256-of-canonical-UTF8-payload",
  "error": null
}
```

分析运行通过 `DataSnapshotRecorder` 在数据工具边界记录每个数据集的返回值、来源和状态。Recorder 先把经过 JSON canonicalization（UTF-8、排序键、无多余空白）的 payload 写入受控 `snapshots/` 根目录临时文件，再 fsync 后原子 rename；`sha256` 对该 canonical UTF-8 字节计算。`SnapshotStore` 只允许解析 `payload_ref` 相对于该受控根目录的路径，并拒绝绝对路径、`..` 和符号链接逃逸。

每个数据工具都必须通过 `SnapshotAwareDataProvider` 获取数据：先读取当前运行的 snapshot manifest，命中时只读取并校验 payload hash，不再次请求供应商；未命中时才通过 `ProviderRouter` 抓取并记录。`TradingAgentsGraph` 构造时必须接收当前运行的 `SnapshotStore`/manifest，所有 MVP 工具调用都从这个 provider 入口取得数据；任何工具绕过入口都使运行失败并标记 `reproducibility: unavailable`，不得生成“可复查”报告。

运行级 manifest 固定保存为发布目录内的 `snapshots/{run_id}/manifest.json`。报告临时目录为 `reports/.tmp/{run_id}/`，包含 `complete_report.md`、`run.json`、`COMMITTED`、manifest 和所有 dataset payload；完整目录原子 rename 到受控报告根目录后，`data_snapshot_id` 指向该目录内的 manifest。manifest 包含 `schema_version`、`run_id`、`created_at`、`completed_at` 和 `datasets[]`；dataset payload 保存为 `snapshots/{run_id}/datasets/{dataset_key}.json`。JSON 使用 UTF-8、递归排序键、无空白、统一 `null`；DataFrame 按列名排序、按行索引稳定排序，日期转 UTC ISO-8601，NaN/Inf 转 `null`，浮点使用 Python `repr`；CSV 先解析为记录数组；Markdown/纯文本统一换行符为 `\\n`。`sha256` 对最终 canonical UTF-8 字节计算。快照读取失败、manifest schema 不匹配或 hash 不匹配统一使运行进入 `failed`，错误码为 `snapshot_corrupt`，不使用实时数据补救。报告生成只读取本次运行的 manifest 和已抓取结果，不在渲染报告时重新抓取行情。`data_snapshot_id` 是运行级 manifest ID，endpoint 返回该 ID 下的 dataset 列表。

`news`、`sentiment` 包含 Yahoo/StockTwits/Reddit，`prediction_market` 对应 Polymarket。部分数据集失败时，快照状态为 `partial` 或 `unavailable`，报告必须显示缺失项，模型不得补造数值。快照 manifest 在报告完成后不可更新；同一运行的幂等键包含规范化代码、分析日期、数据集、供应商链和配置版本。报告目录只有同时存在 `COMMITTED`、完成状态 `run.json`、数据库运行记录为 `completed`、manifest schema 正确且所有 dataset payload 的 SHA-256 校验通过时才被 history 索引；任一条件缺失都不展示。目录 rename 后到写入 `COMMITTED` 前崩溃，或数据库提交失败、manifest 缺失/校验失败时，整个目录移动到 `quarantine/` 并记录原因，绝不索引；quarantine 中的目录可安全回收。

## 8. API 草案

### 关注列表（MVP）

MVP 启动时幂等创建固定列表 `default`（名称“我的关注”），不提供多列表 CRUD。实际路由为：

- `GET /api/watchlist`：返回默认列表和项目；
- `POST /api/watchlist/items`：加入规范化后的资产，重复项目返回 HTTP 409；
- `PATCH /api/watchlist/items/{item_id}`：只更新排序和备注，不允许改规范化代码；
- `DELETE /api/watchlist/items/{item_id}`：删除成功返回 204（无响应体），不存在返回 404；
- `POST /api/watchlist/reorder`：提交完整 item ID 顺序，缺失或重复 ID 返回 HTTP 422。

`item_id` 是数据库生成的稳定 ID。加入资产前先规范化代码，规范化代码在默认列表内唯一；`BTCUSD` 和 `BTC-USD` 因此只能保留一个条目。未来多列表扩展时再增加带 `watchlist_id` 的 CRUD 路由，不改变项目 ID 语义。

除 DELETE 外，响应统一为 `{ "watchlist": {"id":"default","name":"我的关注","version":3}, "items": [...] }`；写操作成功返回更新后的资源和递增 `version`。排序更新必须携带当前 `version`，版本不匹配返回 HTTP 409，避免并发拖拽覆盖；加入重复规范化代码返回 HTTP 409；所有请求体校验失败返回 HTTP 422。

### 行情

- `GET /api/quotes?symbols=AAPL,0700.HK,BTC-USD`
- `GET /api/assets/{symbol}/candles`
- `GET /api/assets/{symbol}/identity`
- `GET /api/providers/market-data`

- `GET /api/settings`：只读返回配置来源、行情策略目录、当前行情策略 ID、供应商链和缓存 TTL；MVP 不提供写入设置接口。

分析请求合同中 `quote_strategy_id` 为可选字段；服务端按请求显式值，否则按 `TRADINGAGENTS_QUOTE_STRATEGY`，否则 SQLite `quote_strategy_id`，否则 `default-yfinance` 解析。`GET /api/config`、`GET /api/settings`、快速分析表单和 runner 都展示同一个解析后的 `effective_quote_strategy_id` 和 `effective_output_language`，避免页面显示值与实际运行值分叉。

行情响应模型（`unavailable` 时所有数值字段为 `null`；`candles` 默认 `1d`，日期范围最多 2 年、最多 2000 个点，超限返回 422；`identity` 无法解析返回 404）：

```json
{
  "items": [{
    "symbol": "AAPL",
    "canonical_symbol": "AAPL",
    "asset_type": "stock",
    "name": "Apple Inc.",
    "currency": "USD",
    "price": 200.99,
    "previous_close": 201.10,
    "change": -0.11,
    "change_percent": -0.0547,
    "market_status": "open|closed|unknown",
    "source": "yfinance",
    "quote_time": "2026-08-27T20:00:00Z",
    "fetched_at": "2026-08-27T20:00:03Z",
    "freshness": "fresh|delayed|stale|unavailable",
    "is_delayed": false,
    "error": null
  }],
  "partial": false
}
```

`symbols` 最多 50 个，超过返回 HTTP 422；单项失败使用 `{ "code": "timeout|not_configured|no_data|invalid_symbol|provider_error", "message": "中文诊断" }`，全量失败仍返回 HTTP 200 和 `partial: true`，由每个 item 的 `freshness=unavailable` 表示。缓存命中返回原始 `quote_time` 和新的 `fetched_at`，TTL 超过后返回 `freshness=stale`。`is_delayed=true` 只允许与 `freshness=delayed` 或 `stale` 同时出现；实时数据使用 `freshness=fresh` 且 `is_delayed=false`。所有 HTTP 时间字段为 UTC ISO-8601。`GET /api/providers/market-data` 返回 `{ "providers": [{"id":"yfinance","installed":true,"configured":true,"capabilities":["quote","candles","identity"],"status":"ready|not_configured|error","reason":null}] }`，不返回密钥或端点。`GET /api/settings` 返回 `{ "schema_version": 1, "fields": { "quote_strategy_id": {"value":"default-yfinance","source":"env|sqlite|default"}, "quote_provider_chain": {"value":["yfinance"],"source":"env|sqlite|default"}, "quote_ttl_seconds": {"value":60,"source":"env|sqlite|default"}, "output_language": {"value":"Chinese","source":"env|sqlite|default"} }, "strategies": [{"id":"default-yfinance","providers":["yfinance"],"available":true}] }`。SQLite migration 启动时先创建 `schema_version` 表，再按版本顺序执行；任一 migration 失败在事务中回滚，服务拒绝启动并保留旧版本。

`GET /api/assets/{symbol}/candles` 接受 `interval=1d|1h|15m`、`start`、`end`，返回 `{ "symbol": "AAPL", "interval": "1d", "items": [{"time":"2026-08-27T00:00:00Z","open":200.0,"high":202.0,"low":199.0,"close":201.0,"volume":1000}], "source":"yfinance", "fetched_at":"...", "freshness":"fresh|stale|unavailable", "error": null }`；`identity` 返回 `{ "symbol": "AAPL", "canonical_symbol":"AAPL", "name":"...", "asset_type":"stock", "exchange":"...", "currency":"USD", "source":"yfinance", "error": null }`，无法解析时字段为空且 HTTP 404。

### 分析和报告

保留现有 `/api/runs`、事件流和 `/api/history`，扩展报告元数据以引用运行级 `data_snapshot_id` 和聚合 `data_status`。dataset `status` 固定为 `complete|partial|unavailable`，`freshness` 固定为 `fresh|delayed|stale|unavailable`；聚合规则是：任一 dataset `status=unavailable` 为 `unavailable`，否则任一为 `partial` 为 `partial`，否则任一 `freshness=stale` 为 `stale`，其余为 `complete`。没有 manifest 的 legacy 报告返回 `data_status: "unknown"`，不参与 freshness 筛选但仍可打开。增加 `GET /api/history/{report_id}/data-snapshot` 返回该运行级 ID 下的 dataset manifest 列表；旧报告没有 manifest 时返回 404 和中文“来源信息不可用”。分析请求的完整新增字段为 `{ "quote_strategy_id": "default-yfinance|fallback-yfinance-alpha-vantage", "provider": "openai", "quick_model": "gpt-5.4-mini", "deep_model": "gpt-5.5", "output_language": "Chinese" }`，其中 `quote_strategy_id` 必须存在于服务端策略目录。完成后的 `run.json` 和 history detail 必须包含 `quote_strategy_id`、`effective_quote_provider_chain`、`data_snapshot_id`、`data_status`、`reproducibility`。请求不提交 API Key、端点或任意网络地址。

## 9. 验收标准

- 中文界面中不出现英文导航、按钮、错误信息或“研究台进度”残留在完成报告头部；
- 关注列表可以添加、排序、删除资产，删除后报告仍可访问；
- 每条行情都能显示价格时间、抓取时间、数据源和延迟/过期状态；
- 主供应商失败时只按显式配置的备用链切换，并在响应中记录实际来源；缺少依赖或凭据时可诊断为 `not_configured`；
- 行情全部不可用时页面可恢复，且不会显示伪造价格；
- 从关注列表可以直接发起分析，分析日期和数据快照可追溯，manifest 字段和 SHA-256 可验证且完成后不可变；
- 报告页只显示报告内容和来源，不显示运行进度组件；
- 旧报告没有数据快照时仍可打开，并显示“来源信息不可用”；
- 重启 Web 服务不会让已完成报告消失；
- 现有 `web_runs` 数据在服务重启和迁移测试中保留，SQLite 实现通过仓储接口测试，后续替换数据库无需改动页面契约；
- 取消与完成竞态测试保证只有一个终态，报告目录和运行记录不会出现半提交；
- `GET /api/runs/{id}`、事件流和历史列表对 `interrupted` 有稳定合同，且可从中发起新分析；
- `GET /api/assets/{symbol}/candles` 对周期、日期范围和点数限制有确定验证；`unavailable` 数值字段为 `null`；
- migration 有 baseline/schema version、事务回滚和旧 `web_runs`/报告目录保留测试；
- baseline 为 schema version `1`：保留现有 `web_runs` 全字段，新增 `watchlists`、`watchlist_items`、`market_quotes`、`market_candles`、`analysis_data_snapshots`、`web_run_events`、`schema_version`；迁移使用进程级锁和 SQLite `BEGIN IMMEDIATE`，失败回滚。启动时先迁移数据库，再扫描已存在报告目录生成只读索引；数据库提交成功但文件发布失败时将运行标记 `failed` 并隔离目录，文件发布成功但数据库提交失败时移动到 `quarantine/`，后续恢复任务按 `COMMITTED` 和数据库状态补偿或回收；
- 桌面和窄屏均可用，表格不破坏页面布局，键盘和屏幕阅读器可访问。

## 10. 实施顺序

### MVP

1. 为现有 `web_runs`、报告目录和新增关注/行情表建立版本化 SQLite migration 与仓储接口；
2. 为 yfinance/Alpha Vantage 建立 `QuoteProvider`、`ProviderRouter`、统一 DTO、缓存和故障语义；
3. 增加默认关注列表 API、行情 API 和中文首页；
4. 增加资产详情和从关注列表发起分析；
5. 在分析数据工具边界记录 `analysis_data_snapshots` manifest，并以原子协议提交报告和元数据；
6. 重做中文报告阅读页、报告库和来源快照查看；
7. 增加供应商故障、过期数据、重启恢复、取消竞态和移动端测试。

### 后续扩展

1. 以同一 `QuoteProvider` 协议加入 Polygon、Twelve Data、Tushare、AKShare；
2. 加入多关注列表、WebSocket、Redis 和后台任务队列；
3. 将 SQLite 仓储迁移到 PostgreSQL，报告正文迁移到对象存储；
4. 增加只允许白名单字段的非密钥设置编辑。
