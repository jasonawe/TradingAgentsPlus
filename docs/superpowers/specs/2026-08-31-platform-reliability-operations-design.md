# TradingAgentsPlus 平台可靠性与可观测性改进设计

**日期：** 2026-08-31  
**范围：** Ruff/运行时告警、分析任务可靠性、报告索引分页、行情源健康度、中文多语言

## 目标

在不改变 TradingAgents 核心分析算法和既有 API 使用方式的前提下，提升 Web 平台的任务可靠性、数据可追溯性、报告列表性能和中文界面一致性。改动应保持本地单用户部署开箱即用，并为后续多 Worker、PostgreSQL 和多用户部署保留清晰边界。

## 现状与约束

- Web 运行状态、关注列表、行情缓存和设置已经使用 SQLite Repository。
- 报告正文保存在文件系统，历史列表目前递归扫描报告目录。
- 分析任务由单个后台 Worker 执行，浏览器通过 SSE 接收进度。
- 行情默认来自 yfinance，可选 Alpha Vantage 作为备用源。
- 前端为无构建步骤的原生 JavaScript；页面文案已经以中文为主，但 API 标签和部分动态值仍有英文。
- 不能破坏现有 `/api/...` 路径、报告文件格式、历史报告兼容性和 CLI 分析流程。

## 设计总览

### 1. 质量基线与告警

在当前 Ruff 配置下清理全仓库 lint 错误，替换已弃用的 Starlette 常量，并将必要的运行时告警转换为明确、可测试的行为。CI 保持 Ruff、compileall 和 pytest 为合并门槛；浏览器 smoke 测试继续作为可选依赖任务运行。

不扩大本次范围去重写 CLI 或引入新的前端构建系统。只做能降低当前维护风险的局部重构，例如整理导入、拆除无效变量和移除重复赋值。

### 2. 分析任务心跳、超时和终态校验

扩展 `web_runs` 记录：

- `progress`：0 到 1 的持久化进度
- `last_heartbeat_at`：Worker 最近一次活动时间
- `timeout_at` 或等价的截止时间
- `terminal_reason`：完成、失败、取消、超时、中断的稳定原因码；它是 canonical reason，旧 `error_code` 保留为兼容别名

本期固定新增终态 `timed_out`，对应事件 `run_timed_out`、`terminal_reason=heartbeat_timeout`、HTTP 查询仍返回 200。旧客户端把未知终态按“已结束”展示；前端和报告库明确将它从进行中集合排除。

状态转换契约：

| 当前状态 | 允许转换 | 触发 | 终态事件 |
| --- | --- | --- | --- |
| queued | running / timed_out / cancelled | Worker 开始、watchdog、用户取消 | `run_started` / `run_timed_out` / `run_cancelled` |
| running | publishing / failed / cancelled / timed_out / interrupted | 报告发布、异常、用户取消、watchdog、服务重启 | 对应终态事件 |
| publishing | completed / failed / interrupted | 发布 gate 成功、失败或服务重启恢复 | `run_completed` / `run_failed` / `run_interrupted` |
| completed/failed/cancelled/timed_out/interrupted | 不再转换 | 幂等读取 | 不重复发布 |

所有时间字段保存 RFC3339 UTC，进度必须单调不减并限制在 `[0,1]`；收到迟到的较小进度时丢弃该更新并保留当前值，不让任务失败，也不发布倒退事件。终态事件强制 `progress=1.0` 仅适用于 `completed`，其他终态保留最后有效进度。

`RunManager` 保持现有单活动任务约束，但增加：

1. Worker 在阶段切换、消息发布和每 15 秒（可配置）写入心跳。心跳只能由实际执行分析的 Worker 线程写入，不使用独立“假心跳”线程；心跳用于发现阶段之间的失活，不能阻止一个已经阻塞的模型调用。默认任务最大运行时间 2 小时，心跳超时阈值 180 秒，心跳间隔 15 秒；任务最大运行时间范围是 5 分钟至 24 小时，心跳间隔范围是 5 秒至 60 秒，心跳超时范围是 30 秒至 10 分钟。
2. `RunManager` 启动一个生命周期绑定的 watchdog 线程，每 15 秒检查一次：固定的 `timeout_at` wall-clock deadline 是阻塞调用的最终上限，heartbeat lease 只处理已回到 Worker 控制面的失活。配置键为 `run_timeout_seconds`、`run_heartbeat_interval_seconds`、`run_heartbeat_timeout_seconds`，优先级为环境变量 > SQLite 设置 > `DEFAULT_CONFIG` > 默认值；本期不把这些字段加入 `AnalysisRequest`，避免破坏现有 extra=forbid 契约。创建任务时把最终值写入 `web_runs`，重启后沿用任务自身的值。Provider/LLM 调用接收剩余 deadline 并设置不超过该值的请求 timeout；Worker 从阻塞调用返回后必须通过终态 CAS，拒绝对 `timed_out` 任务 late complete/publish。读取状态、写心跳和 watchdog 都调用同一个 CAS 终态函数。`queued/running` 才能因 deadline 或 heartbeat 转为 `timed_out`；`publishing` 不会被 heartbeat 误杀，但服务启动恢复时会处理它。成功转换后只发布一次 `run_timed_out`。
3. 终态统一校验：`completed` 必须满足 `COMMITTED`、`complete_report.md` 和 `run.json.status=completed`；SQLite 报告索引是最终一致的 read index，不作为文件发布 gate。`completed` 时 `progress=1.0`、`current_agent` 清空；文件 gate 不满足时转为 `failed`，`terminal_reason=publish_incomplete`。失败、取消、超时、中断的 `progress` 保留最后值，`current_agent` 清空，并且不得继续显示进行中状态。
4. 服务启动恢复 `publishing`：若最终目录已存在完整 `COMMITTED`、`complete_report.md` 和 completed sidecar，则 CAS 为 `completed` 并补建索引；否则将该目录移入受控 orphan 区并 CAS 为 `failed`，`terminal_reason=publish_incomplete`。queued/running 统一 CAS 为 `interrupted`，保证任何重启后任务不会停留在非终态。
5. SSE 断线重连读取同一 SQLite 事务切点下的状态版本 `snapshot_seq`，返回带 `snapshot_seq` 和 `replay_from_seq=snapshot_seq+1` 的 `run_snapshot` 数据事件，再从 `replay_from_seq` 消费事件。快照不占用全局事件序号，SSE `id` 只使用真实事件 `seq`；客户端收到快照后将本地状态覆盖为快照并把 `lastSeq` 设为 `snapshot_seq`，收到真实事件后才继续推进。若内存环已淘汰游标之前的事件，服务只保证最新快照和之后事件；run 行与终态 event 在同一 SQLite 事务中提交，启动恢复会补发缺失终态事件。旧客户端未监听新事件时，`GET /api/runs/{id}` 返回终态，前端轮询兜底将未知状态按已结束处理。

运行记录对外至少包含 `progress`、`phase`、`current_agent`、`last_heartbeat_at`、`timeout_at`、`terminal_reason` 和 `error_code`。超时事件示例：

```json
{"event":"run_timed_out","run_id":"run-1","seq":18,"payload":{"status":"timed_out","progress":0.45,"terminal_reason":"heartbeat_timeout","error_code":"heartbeat_timeout","error_message":"分析任务超过心跳等待阈值"}}
```

现有状态值保持兼容；如新增 `timed_out`，客户端和历史列表必须将其作为终态处理。服务重启仍不能恢复已发出的模型请求，但会记录中断原因和最后心跳，避免界面显示陈旧的“正在准备分析”。

### 3. SQLite 报告索引与分页

新增 `reports` 表（migration v2），以 `report_id` 为唯一键，字段类型和约束固定如下：

```sql
CREATE TABLE reports (
  report_id TEXT PRIMARY KEY,
  run_id TEXT,
  ticker TEXT,
  asset_type TEXT,
  analysis_date TEXT,
  generated_at TEXT,
  status TEXT NOT NULL DEFAULT 'completed',
  rating TEXT,
  signal TEXT,
  output_language TEXT,
  summary_status TEXT,
  decision_preview TEXT,
  data_snapshot_id TEXT,
  provider TEXT,
  quick_model TEXT,
  deep_model TEXT,
  analysts_json TEXT,
  research_depth INTEGER,
  data_status TEXT,
  reproducibility TEXT,
  quote_strategy_id TEXT,
  effective_quote_provider_chain TEXT,
  root_name TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'web',
  index_status TEXT NOT NULL DEFAULT 'indexed',
  path_state TEXT NOT NULL DEFAULT 'valid',
  updated_at TEXT NOT NULL,
  UNIQUE(root_name, relative_path)
);
CREATE INDEX idx_reports_generated ON reports(generated_at, report_id);
CREATE INDEX idx_reports_ticker ON reports(ticker);
CREATE INDEX idx_reports_status ON reports(status);
CREATE INDEX idx_reports_analysis_date ON reports(analysis_date);
```

migration v2 在同一事务中幂等执行以下旧表兼容步骤：

```sql
ALTER TABLE web_runs ADD COLUMN last_heartbeat_at TEXT;
ALTER TABLE web_runs ADD COLUMN timeout_at TEXT;
ALTER TABLE web_runs ADD COLUMN terminal_reason TEXT;
ALTER TABLE web_runs ADD COLUMN run_timeout_seconds INTEGER;
ALTER TABLE web_runs ADD COLUMN run_heartbeat_interval_seconds INTEGER;
ALTER TABLE web_runs ADD COLUMN run_heartbeat_timeout_seconds INTEGER;
CREATE TABLE IF NOT EXISTS report_index_outbox (
  report_id TEXT PRIMARY KEY,
  root_name TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT NOT NULL
);
```

每个 `ALTER` 先用 `PRAGMA table_info` 判断列是否存在；迁移失败整体回滚且 schema version 不递增。旧 `web_runs` 行的 heartbeat/deadline 列允许 NULL，只有新任务启用超时。`provider_health` 表和 `reports` 表在同一 migration 事务中创建。

允许值：`root_name ∈ {web_reports, results_reports, cwd_reports}`，`source ∈ {web, legacy}`，`status ∈ {completed, failed, cancelled, interrupted, timed_out}`，`index_status ∈ {indexed, pending, error}`，`path_state ∈ {valid, missing, unsafe}`。`rating` 是 canonical，`signal` 与它保持同值以兼容旧客户端。时间统一 RFC3339 UTC；legacy 报告没有时间时 `generated_at=NULL`，排序时按最旧处理并以 `report_id` 稳定排序。`decision_preview` 保存清洗后的最多 512 个 Unicode 字符，用于分页搜索。

报告正文文件系统是 canonical source，SQLite `reports` 是可重建的 read index；文件 rename、`COMMITTED` 和索引提交不能假定为跨系统事务。发布协议为：临时目录写完并校验 gate -> 原子 rename -> SQLite upsert；若 upsert 失败，则在同一可用数据库事务中写入 `report_index_outbox`，列表请求将该 outbox payload 作为短期内存 overlay 合并返回，保证正文不消失；数据库完全不可用时记录日志，服务启动时受控回填。后台每 30 秒重试 outbox，成功后删除 outbox。回填和 outbox 写入时同步维护 `path_state`；列表查询只读取 `path_state=valid`，文件被删除后由下一次回填标记 `missing`，越界/符号链接标记 `unsafe`，不在请求时扫描文件。overlay 先按相同 `report_id` 覆盖索引行，再按同一排序/过滤 SQL 规则合并，total 只计算去重后的 valid 记录。列表请求不触发全量扫描；服务启动和显式 rebuild 执行受控回填，按 `root_name/relative_path` 去重。索引缺失期间报告不标记为分析失败。

`/api/history` 保持无参数调用的兼容行为，同时支持：

- `page`、`page_size`
- `query`/`ticker`
- `status`
- `asset_type`
- `date_from`、`date_to`
- `sort`

当请求不带任何分页/筛选参数时，严格返回原有 JSON list；只要带任一新参数，就返回 `{items,page,page_size,total,has_next}` envelope。`page` 默认 1、范围 1..100000，`page_size` 默认 20、范围 1..100；`sort` 只允许 `generated_at_desc`、`generated_at_asc`，默认降序并以 `report_id` 作为稳定 tie-breaker。`ticker` 提供时执行不区分大小写的精确匹配并忽略 `query`；否则 `query` 对 ticker 和 `decision_preview` 做不区分大小写包含匹配。`date_from/date_to` 过滤 `analysis_date`，两端均包含，按 UTC 的 ISO 日期解释，非法范围返回 422。文件缺失、根目录不在 allowlist 或相对路径越界的索引行标记不可见并从 total 排除。无结果或页码超出范围均返回 200 和空 `items`、`has_next=false`。前端报告库维护 `page/pageSize/total`，使用“上一页/下一页”控件；收到 envelope 或旧 list 都先归一化为同一内部结构。

### 4. 行情数据源健康度与缓存过期

在 ProviderRouter/QuoteService 层统一记录每个供应商的 `provider_health`（SQLite 持久化）并统一记录：

migration v2 增加：

```sql
CREATE TABLE provider_health (
  provider TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'not_configured',
  window_started_at TEXT,
  request_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_success_at TEXT,
  last_failure_at TEXT,
  last_latency_ms REAL,
  last_error_code TEXT,
  last_error_message TEXT,
  updated_at TEXT NOT NULL
);
```

`status` 的聚合窗口固定为最近 5 分钟；0 请求保持 `not_configured` 或上一状态，成功后立即恢复 `ready`，连续失败 3 次或窗口失败率大于 50% 为 `degraded`，配置错误/无凭证为 `not_configured`，其他不可恢复错误为 `error`。供应商级健康度不携带 symbol 级缓存状态；缓存状态只存在行情响应和 `market_quotes`。

窗口算法：每次更新前若 `now >= window_started_at + 300s`，先将窗口起点设为当前时间并把 request/failure/consecutive_failures 计数全部清零；成功请求将 `consecutive_failures` 清零并立即恢复 `ready`，失败请求递增。只有当前请求明确为配置错误时才是 `not_configured`，只有当前请求明确为不可恢复错误时才是 `error`；否则按当前窗口连续失败 3 次或失败率大于 50% 计算 `degraded`，其余为 `ready`。服务重启从持久化窗口继续，过期窗口在首次更新时轮转。fake-clock 测试覆盖恰好 300 秒边界、轮转清零和成功后的恢复。

- `status`：`ready`、`not_configured`、`degraded`、`error`；`degraded` 表示最近 5 分钟失败率大于 50% 或连续失败 3 次
- 最近成功时间、最近失败时间
- 最近请求耗时
- 错误码和安全的用户可读诊断
- 当前缓存状态、数据时间和过期秒数

行情响应继续保留现有价格字段，并增加或规范化：`freshness`（兼容既有 `fresh`/`delayed`/`stale`/`unavailable`，本期不删除 `fresh`）、`cache_status`（兼容既有 `fresh`/`stale`，新增 `live`/`hit`/`miss` 只作为补充）。`cache_status=stale` 与 `freshness=stale` 等价；`cache_status=hit` 表示命中缓存但不改变 freshness。`quote_time/as_of` 是市场数据时间，`fetched_at` 是抓取时间；`stale_seconds = max(0, now - quote_time)`，没有 quote_time 时为 null。缓存命中不重置 quote_time 和 stale_seconds。主源失败但缓存可用时返回 `freshness=stale`；无数据且无缓存时返回 `freshness=unavailable`；备用源成功时返回 `freshness=fresh` 或 `delayed` 并标明实际 source。

行情状态矩阵：

| provider 结果 | 缓存 | freshness | cache_status |
| --- | --- | --- | --- |
| 当前源成功，数据时间在 TTL 内 | 否 | `fresh` | `live` |
| 当前源成功，市场自身带延迟标记 | 否 | `delayed` | `live` |
| 当前源失败，缓存存在但仍在 TTL 内 | 是 | `fresh` | `hit` |
| 当前源失败，缓存存在且超过 TTL | 是 | `stale` | `hit` |
| 当前源和缓存都不可用 | 否 | `unavailable` | `miss` |

前端 5 秒刷新增加 4 秒请求超时和 AbortController；失败采用 5 秒、10 秒、20 秒、最高 60 秒的指数退避，成功后复位。新请求取消旧请求，旧响应不得覆盖新响应；失败保留最后成功数据并显示过期提示。页面显示“实时、延迟、缓存、已过期、不可用”等中文状态。

### 5. 中文多语言资源

本期只实现并固定 `zh-CN` UI locale，保留资源结构为未来扩展 `en-US`；不新增语言切换入口，也不改变此前“中文固定界面”的规格。稳定 key 与显示文本分离，但通过双字段保持 API 兼容：

- API 继续返回现有 label/raw 字段，同时可增加稳定 key 字段；旧客户端不被迫显示 key
- 前端本期只根据 `zh-CN` 资源渲染中文
- 分析输出语言仍由分析请求单独选择，不与 UI locale 混用
- 中文资产名称和市场名优先；没有映射时回退英文原值
- 投资评级、行情状态、错误信息和任务终态在页面、报告卡片、设置页统一映射

默认 UI locale 为 `zh-CN`，不改变现有中文用户体验。未知 key、null 和空字符串必须安全回退原值或统一占位文案，不能渲染为 `undefined`。

## 数据流

```text
分析请求
  -> RunManager 创建 SQLite 任务
  -> Worker 执行并写入心跳/进度/事件
  -> 报告临时目录写入
  -> COMMITTED + SQLite reports 索引
  -> SSE/刷新接口读取最新快照

行情请求
  -> QuoteService 选择 ProviderRouter 链
  -> 记录供应商健康度与缓存状态
  -> SQLite 行情缓存 + API 返回 freshness
  -> 前端 5 秒刷新/失败退避
```

## 错误处理与兼容性

- 数据库迁移必须幂等，旧数据库可以直接启动并自动补列/补表；时间统一保存 RFC3339 UTC。
- 报告索引写入失败不能让已经提交的报告正文变成不可见；应记录可诊断错误并保留扫描回退。
- 超时只能作用于非终态任务，重复检测必须幂等。
- 行情供应商错误不得泄露 API key、Authorization 或原始敏感响应。
- 新增终态、分页字段和健康度字段必须保留旧字段，旧前端仍能正常显示。

## 测试策略

先写失败测试，再实现：

1. `ruff check .`、`python -m compileall -q tradingagents cli web` 和 Starlette 告警回归。
2. 心跳更新、fake clock 超时 CAS、超时转换、完成终态校验、SSE 快照恢复和重启中断。
3. 报告索引 migration v2、历史回填、分页/筛选/排序、参数 422、文件缺失回退和稳定去重。
4. Provider 健康状态、SQLite 持久化、缓存新鲜度矩阵、过期秒数和失败回退。
5. 中文资源覆盖、未知 key/null 回退、评级/市场/终态映射，以及 4 秒超时和退避 fake timer 测试。
6. 现有完整 pytest 套件、JavaScript 语法检查；Playwright smoke 在安装浏览器依赖的 CI job 中作为门禁，缺少可选外部服务的测试必须使用明确 marker 并允许 skip。

## 非目标

- 不在本次改动中引入 Redis、PostgreSQL 或多用户权限系统。
- 不重写 TradingAgents 的 LangGraph 分析流程和提示词。
- 不强制引入 React/Vite/TypeScript。
- 不把报告正文整体迁移进 SQLite。

## 验收标准

- Ruff、compileall、pytest 全部通过，现有跳过项仅因缺少可选外部依赖。
- 长任务能够看到持续更新的心跳和阶段，超过阈值后不会无限显示进行中。
- 完成任务显示 100%，失败/取消/中断/超时显示明确终态。
- 报告库在大量报告下支持分页和筛选，旧报告仍可打开。
- 行情卡片明确显示来源和新鲜度，5 秒刷新不会因慢请求永久阻塞。
- 页面中用户可见的阶段、评级、供应商、市场和错误信息在中文 locale 下不出现不必要英文或 `undefined`。
