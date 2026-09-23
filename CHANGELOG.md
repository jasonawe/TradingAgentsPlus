# Changelog

All notable changes to TradingAgents are documented here.

## [0.4.27] — 2026-09-23

### Fixed
- **create_alert args default was invalid Literal (§0.4.27)**: `_alert_create_args` 的兜底分支返回 `kind="price"` — 不在 `CreateAlertArgs.kind` 的 Literal (`price_above` / `price_below` / `change_pct` / `volume_spike`) 里,触发 `args coerce failed: kind: Input should be 'price_above' / ...`。复现场景:`完整分析:基础面+估值+新闻+近期走势+同业对比+监控告警` — 用户没说具体阈值,planner 走到了 (ALERT, CREATE) 路径,默认 `kind="price"` 在 Pydantic 验证层直接报错。

  修复后兜底 `kind="price_above"` (Schema 自己的 default),配合 `params={}`,HITL gate 给用户弹"价格涨破 ¥0"的提示,他们可以直接取消。同时打 `LOGGER.warning` 记录空槽位事件,留给后续 planner team 把 "监控告警 无阈值" 在 classify() 层降级为 (ALERT, LIST)。

  已知未修(留作后续 workstream):LLM planner 在"基础面+估值+新闻+近期走势+同业对比"这种多维度请求下只跑 2 个 `get_quote`,未 fan-out 到 fundamentals / history / news / list_alerts / 同业 quote。完整补齐需要重写 _build_plan_prompt 或者在 classify() 里加 keyword→tool 映射。

### Tests
- 新增 `tests/test_step63_alert_create_args.py` (19 cases):empty slots → Literal-valid kind + warning;direction+threshold → 正常 price_above / price_below;scope / symbol fallback / asset_type 默认;参数化回归矩阵覆盖 8 种中文/英文短语,确保 `kind="price"` 永远不出现。

## [0.4.26] — 2026-09-23

### Fixed
- **harness.js cache bump (§0.4.26)**: 用户反馈 §0.4.25 multi-intent display_html 改动在浏览器看不到 — `harness.js?v=20260923-harness-retry` cache key 与 §0.4.24 retry 那次共用，导致新 JS 被浏览器命中 stale cache。Cache key 升到 `20260923-harness-display-html-0.4.25`，强制重新加载。验证：`GET /harness/` 返回的 script tag 已是新 key，curl 拉下来 md5 与 `web/static/harness.js` 一致。

## [0.4.25] — 2026-09-23

### Feature
- **multi-intent display_html (§0.4.25.1)**: `_run_multi` 在每个 `tool_result` emit 前统一调用 `_attach_display_html(tool_name, payload)`。前端 multi-intent bubble 优先用 `s.result.display_html`（card 正则匹配 `(history|compare|quote|fundamentals|news|alpha|ack|error)-card`），回退到 `formatRawResult` pipe-table，避免重复转义。
- **retry button SSE reset (§0.4.24.1)**: `sendMessage` 加 `finally` 块 — 设置 `[data-action="retry-tool"]` 按钮 `disabled=false`、恢复 `🔄 重试` 文案。前后端网络抖动时按钮不会卡死。
- **tool metadata auto-mapping (§0.4.27)**: `_intent_for_tool` 优先查 `tool.schema.metadata['display_view']` / `['capabilities']`，fallback 才是硬编码 `_TOOL_INTENT_MAP`。新增 read 工具不用动 short_circuit.py，只要 metadata 写好就自动出 friendly card。

### Refactor
- **`_TOOL_INTENT_MAP` 去重**：之前 commit 0a72546 + 3798c3c 留了两份定义，合并为一份完整的 fallback map（25 个工具）。Metadata path 和 fallback path 同一份默认值，行为一致。


### Tests
- 新增 `tests/test_step59_display_view_matrix.py`（23 cases）：`display_view_for` 对 9 种 intent × 2–3 种 payload shape 的覆盖矩阵；并锁定前端 bubble regex 必须含 `quote/fundamentals/history/news/alpha/ack/error` 7 个 card class。
- 新增 `tests/test_step60_multi_intent_display_html.py`（7 cases）：mock ToolRegistry + 2 个 read tools，触发 multi-intent 路径，断言每个 tool_result payload 都有 `display_html` 且 card class 正确；失败工具不中断 batch。
- 新增 `tests/test_step61_retry_button_reset.py`（4 cases）：源文件 regex 锁定 `sendMessage` finally 块 + 重试按钮 listener 行为 + `node -c` 语法校验。
- 新增 `tests/test_step62_tool_metadata_intent.py`（9 cases）：mock 2 个 tool 验证 `_intent_for_tool` 优先用 metadata，legacy map 仅做 fallback。

## [0.4.24] — 2026-09-23

### Feature
- **error card retry button (§0.4.24)**: 错误卡片右下角加 `🔄 重试` 按钮。点击重发最近一次 user message（`state.lastUserMessage`）——后端会重新走 Tier 1 短路 / Tier 2 规划，可能拿到新数据。
- **frontend prefers ``display_html`` (§0.4.25)**: 前端 `appendToolResult` 检测 `payload.result.display_html`，识别 `*-card` 根类后直接 innerHTML，不再走 `formatRawResult` pipe-table 路径（避免重复逻辑）。
- **backend emits ``display_html`` (§0.4.25)**: `short_circuit.py` 在每次 `tool_result` emit 前调用 `_attach_display_html(tool_name, payload)`，通过 `_TOOL_INTENT_MAP` 把 25 个工具名映射到 Capability intent，再用 `display_view_for` 渲染 friendly card 塞进 payload。

### Fixed
- **get_history partial-data (§0.4.26)**: 当所有 provider 都返回 NO_DATA 时，`get_history` 不再 raise，而是返回 `candles=[]` 的 `HistoryResult`。前端 friendly card 显示"暂无历史数据 + 提示（该资产可能刚上市 / 数据源未覆盖 / 退市）+ 数据源"。
- **`_safe_dump` instance method 签名**: 之前 `def _safe_dump(obj)` 漏了 `self`，导致 `self._safe_dump(args)` 报 TypeError（其他测试碰巧用 `@staticmethod` 调用方式才通过）。修正后签名变 `def _safe_dump(self, obj)`。

### Tests
- 新增 `tests/test_step58_display_html_partial.py`（7 cases）。
- 更新 `tests/test_step50_history_sparkline.py::test_render_card_empty` 适配新文案（"暂无历史数据"）。

## [0.4.23] — 2026-09-23

### Fixed
- **error card for tool failures (§0.4.22.fix)**: tool 失败时（no_data / provider_error / invalid_symbol / rate_limited / timeout）不再返回 raw `❌ tool: no_data: historical candles unavailable` 文本，而是友好 error card（⚠️ 图标 + 工具名 + 标的代码 + 中文错误标签 + 错误码徽章）。三处入口：
  - 后端 `display_view_for` 在 result 含 `error` 字段时短路到 `render_error_card`。
  - 前端 `appendToolResult` 在 `payload.error` 时构造 `renderErrorCard` 而不是返回 raw 文本。
  - `renderMarkdown` trusted class 列表新增 `error-card`。

### Feature
- 新增 `renderers/friendly_cards.render_error_card` + `render_error_card_from_tool_result`，支持 `{error, error_code, symbol}` 顶字段或 `{result: {error, ...}}` 嵌套形态。

### Tests
- 新增 `tests/test_step57_error_card.py`（9 cases）。

## [0.4.22] — 2026-09-23

### Fixed
- **get_history lookback (§0.4.19.fix)**: §0.4.19 引入 `lookback_days` 时把 `start`/`end` 序列化成 ISO 字符串传给 provider，但 yfinance 的 `ticker.history(start=..., end=...)` 只接受 datetime 对象，ISO 字符串触发 `unconverted data remains: T03:09:46+00:00` 异常，被 ProviderFailover 当作 transient error 跳过，最终所有 provider 全部 NO_DATA、用户看到 `historical candles unavailable`。修复后 `get_history` 直接传 datetime 对象（start = now − lookback_days × step × 1.5，end = now）。

### Tests
- 在 `tests/test_step51_history_lookback.py` 新增 2 cases：`test_get_history_passes_datetime_objects`（断言 datetime 类型 + timezone-aware + 时长正确）、`test_get_history_with_explicit_start_passes_through`（断言显式 start/end 不会被 lookback_days 覆盖）。

## [0.4.21] — 2026-09-23

### Feature
- **SPA /harness rail (§0.4.21)**: `index.html` 里的 `#harness-view` 现在带 `#harness-sidebar-toggle` + rail 容器（`#harness-rail-new/-expand/-count/-active-title`）+ i-plus/i-menu icon。SPA 视图的 sidebar 现在能切到 rail 模式。
- **统一友好卡片 (§0.4.22)**: 新增 `renderers/friendly_cards.py`，把 quote / fundamentals / news / alpha / ack 五种 tool result 全部升级为统一卡片样式（共享 `.qc-metrics` / `.fc-metrics` grid、`.m-k` / `.m-v` 关键指标、icon + symbol + name + meta 头部、`.up` / `.down` / `.flat` 涨跌色）。
- **compare intent Tier 1 (§0.4.23)**: `short_circuit._run_compare` 在 `Intent.COMPARE` + ≥2 symbols 时 fan-out 到每个 symbol 调 `get_history`，合并给 `render_compare_card`。`infer_history_params` 自动按用户文案选 interval/lookback，单 symbol 走原 Tier 1 路径不变。

### Fixed
- **renderMarkdown**: §0.4.17/0.4.20 的 trusted-card 检测扩展到 7 种（quote / fundamentals / news / alpha / ack / history / compare），不再被 escape。

### Tests
- 新增 `tests/test_step54_unified_friendly_cards.py`（12 cases）。
- 新增 `tests/test_step55_compare_intent_tier1.py`（3 cases，async）。
- 新增 `tests/test_step56_spa_rail_dom.py`（4 cases）。

## [0.4.20] — 2026-09-23

### Fixed
- **harness sidebar (§0.4.19.fix b)**: 之前默认展开逻辑被包在 ``if (sidebarToggle && layout) { ... }`` 里——但 SPA 的 `/harness` 视图（`index.html` 里的 `#harness-view`）不包含 `#harness-sidebar-toggle` DOM（那是 standalone `harness.html` 路由才有），所以 ``setSidebarExpanded`` 永远不执行、``is-sidebar-expanded`` 类从不落地，session 列表不可见。修复后默认展开决策提到 if 外、toggle click 绑定仍在 if 内。

### Tests
- 更新 `tests/test_step53_sidebar_default_expanded.py` 反映新的源码结构（4 cases）。

## [0.4.19] — 2026-09-23

### Fixed
- **harness sidebar (§0.4.19.fix)**: 从 `/` 进入 `/harness` 时若 localStorage 里有遗留的 ``sidebarExpanded='0'``（早期版本自动写入，无用户主动选择），sidebar 会收起、session 列表不可见。修复后仅当 ``userChoseSidebar='1'``（用户主动点过 toggle）时才尊重该偏好；其它情况默认展开，让 session 列表可见。

### Tests
- 新增 `tests/test_step53_sidebar_default_expanded.py`（3 cases，断言源码决策树）。

## [0.4.18] — 2026-09-23

### Feature
- **history renderer polish (§0.4.18)**: 前端 CSS 美化（`.history-card / .hc-metrics / .m / .up / .down / .hc-chart / .hc-table / .hc-rest`），并让 ``harness.js#renderMarkdown`` 信任以 `<div class="history-card">` 或 `<div class="compare-card">` 开头的 HTML（此前会被 escape）。
- **multi-asset compare (§0.4.20)**: 新增 `renderers/compare_sparkline.render_compare_card`，8 色 palette、多资产归一化到 100、图例 + 表格。
- **hover tooltip (§0.4.21)**: SVG `<polyline>` 携带 `data-points` / `data-series-points`，`harness.js#attachChartTooltip` 装一个 delegated mouseover 监听，悬停显示价格/日期。
- **interval 自适应接通 (§0.4.19)**: `HistoryArgs.lookback_days` 字段 + `get_history` 按它推导 ISO 起止；`short_circuit._build_args` 在调用 `HistoryArgs` 时根据 message 推断 interval + lookback 并支持显式 slot 覆盖。

### Tests
- 新增 `tests/test_step51_history_lookback.py` (6 cases)。
- 新增 `tests/test_step52_compare_sparkline.py` (5 cases)。
- `tests/test_step50_history_sparkline.py` 仍 18/18 pass。

## [0.4.17] — 2026-09-23

### Feature
- **history renderer**: friendly `get_history` 升级为 SVG sparkline + 关键指标 + 可折叠价格表。
  - 新增 `tradingagents/agent_harness/renderers/history_sparkline.py`。
  - 自适应价格量级（AAPL/513880/加密同一份 SVG 逻辑）；candles < 2 跳过图，缺字段显示"—"。
  - 接入 `tools/display_view.py:_render_history`，替代旧的纯文本摘要。
- **interval 自适应**: 用户文案（"日内"/"周"/"3 个月"/"半年"/"1 年"/"N 天"）→ `(interval, lookback)`。

### Tests
- 新增 `tests/test_step50_history_sparkline.py`（18 cases, all green）。

## [0.4.16] — 2026-09-22

### Fixed
- **tier**: thread `carry_symbols` into `fast_route` so implicit-asset follow-ups
  (e.g. "看一下这个资产最近 30 天价格走势") reach `Tier.DIRECT` instead of
  falling through to `PLAN_EXECUTE`.
  - `fast_route_with_op(msg, carry_symbols=...)` 之前调用 `fast_route(message)`
    时丢了 `carry_symbols`，导致 Tier.DIRECT 的 `(HISTORY/QUOTE/...) and symbols`
    谓词失败。
  - `fast_route(message, carry_symbols=None)` 新增可选参数；当消息抽取为空且
    `carry_symbols` 非空时沿用。
  - ~15 LoC in `tradingagents/agent_harness/core/tier.py`.

### Tests
- 新增 `tests/test_step49_carry_forward_fast_route.py` (6 cases, all green).

## [0.4.15] — 2026-09-22

`get_fundamentals` tool surfaces real provider data instead of
returning all-None for every numeric field (regression for the
"看一下 AAPL 的基本面数据 → PE: null, PB: null, market cap: null"
report).

### Fixed

- **`get_fundamentals` no longer reads fields off
  `AssetIdentity`.** `tradingagents/agent_harness/tools/builtin.py`
  used to call `get_identity` (which only carries name /
  exchange / currency) and then `getattr(identity, "pe_ratio",
  None)` against a Pydantic model that never declared them. Every
  numeric field silently came back as `None` for every provider,
  every symbol. Now it calls the new
  `Provider.get_fundamentals(symbol, asset_type)` and copies
  the populated fields.
- **New `FundamentalsSnapshot` model** in `web/market_models.py`
  carrying PE / PB / market cap / circulating cap / ROE / revenue /
  net income / EPS / dividend yield / 52-week high-low / source.
  Default values are `None` so providers can fill only what they
  have; the harness tool surfaces missing fields as "未提供".
- **`YFinanceProvider.get_fundamentals` override** reads
  `ticker.info` directly: marketCap → market_cap, trailingPE →
  pe_ratio, priceToBook → pb_ratio, returnOnEquity → roe,
  trailingEps → eps, dividendYield → dividend_yield,
  fiftyTwoWeekHigh → fifty_two_week_high,
  fiftyTwoWeekLow → fifty_two_week_low. Provider-specific
  extras (forward_pe / peg_ratio / beta / profit margin /
  operating margin / debt_to_equity / free_cashflow / sector /
  industry) flow into the `payload` dict for downstream consumers.
- **`Provider.get_fundamentals` default impl** merges
  `get_quote` (akshare + eastmoney fill market_cap / pe_ratio on
  their A-share QuoteSnapshot) with `get_identity`. When *neither*
  layer produces any fundamentals field (only the name comes
  back), it raises `ProviderError(NO_DATA)` so the failover walks
  to the next provider instead of short-circuiting with an empty
  snapshot.
- **`ProviderFailover` treats NO_DATA on `get_fundamentals` as
  transient** (`tradingagents/agent_harness/observability/
  failover.py`). Each provider covers a different universe —
  eastmoney / akshare have A-share fundamentals on the quote
  snapshot, yfinance has US ticker fundamentals from ticker.info.
  Without this, eastmoney raising NO_DATA for AAPL short-circuited
  the chain and yfinance never got a turn. `get_quote` still
  treats NO_DATA as terminal.
- **Expanded `_render_fundamentals` view** in
  `tradingagents/agent_harness/tools/display_view.py` to surface
  the new fields: name, 流通市值, EPS, 股息率, 52周高/低, 货币.
  Renders big numbers (market cap, revenue) with thousand-separators
  and small ratios with 2-decimal precision.

### Tests

- `tests/test_step48_fundamentals_tool.py` — 7 cases covering:
  - model field surface
  - Provider ABC declares `get_fundamentals`
  - YFinanceProvider overrides (not just inherits)
  - default impl raises NO_DATA for non-fundamentals symbols
  - failover walks past eastmoney to yfinance for AAPL
  - harness tool returns real PE / market_cap for AAPL
  - harness tool returns real PE for 600036.SS

### Files

- changed: `web/market_models.py` (new model, +40 LoC)
- changed: `tradingagents/data/providers/base.py` (new default
  `get_fundamentals`, +60 LoC)
- changed: `tradingagents/data/providers/yfinance_provider.py`
  (override + helper, +90 LoC)
- changed: `tradingagents/agent_harness/tools/builtin.py` (tool
  rewrite, +50 LoC)
- changed: `tradingagents/agent_harness/observability/failover.py`
  (per-method NO_DATA handling, +12 LoC)
- changed: `tradingagents/agent_harness/tools/display_view.py`
  (richer rendering, +25 LoC)
- new: `tests/test_step48_fundamentals_tool.py` (181 LoC, 7 cases)
- changed: `pyproject.toml` (version bump 0.4.14 → 0.4.15)

## [0.4.14] — 2026-09-22

Harness sidebar: default to expanded unless the user has explicitly
chosen otherwise (regression for "首次进 /harness 仍然没有 session 列表").

### Fixed

- **Stale `sidebarExpanded='0'` from prior builds no longer traps
  the sidebar in collapsed mode.** `web/static/harness.js` now
  reads a new localStorage flag `ta.harness.userChoseSidebar` that
  is set to `'1'` ONLY when the user actually clicks the sidebar
  toggle, the rail-expand button, or the rail-active title. Until
  that flag is present, the page treats any persisted
  `sidebarExpanded='0'` value as stale (left over from the
  §0.4.10/§0.4.11 builds, a browser sync, or an accidental
  double-click) and **defaults to expanded**.
- **JS cache key bumped** to `20260922-harness-rail10` in
  `web/static/harness.html` and `web/static/index.html` so
  existing browser caches reload the new init logic.

### Why this regressed

- §0.4.11 added the rail-expand button so collapsed users had a
  way back, but the underlying `setSidebarExpanded()` still
  persisted `'0'` immediately on page load (via the init-time
  call). Users whose stale `'0'` predated the §0.4.11 fix kept
  seeing only the 56-px rail-with-toggle on every subsequent
  visit and concluded the session list was missing.
- §0.4.14 separates "user has expressed a preference" from
  "the page has rendered once": only the former locks the
  state. Once the user actively clicks the toggle, their
  choice is preserved across reloads (same as before); the
  legacy value is only authoritative when paired with the
  new flag.

### Tests

- `tests/test_step47_sidebar_default_expanded.py` — 4 Playwright
  scenarios: fresh user, stale legacy `'0'`, explicit collapse,
  explicit expand. All pass against the live harness.

### Files

- changed: `web/static/harness.js` (~12 LoC added for the
  ``userChoseSidebar`` flag + init guard).
- changed: `web/static/harness.html` + `web/static/index.html`
  (cache-key bump).
- new: `tests/test_step47_sidebar_default_expanded.py` (129 LoC).
- changed: `pyproject.toml` (version bump 0.4.13 → 0.4.14).

## [0.4.13] — 2026-09-22

Harness LLM tool-routing: per-intent planner hint + tool routing cheat
sheet (Plan B + D from the §0.4.12 review of the "走势/N天 → get_quote"
classifier failure and the broader LLM tool-selection question).

### Added

- **`tradingagents/agent_harness/core/tool_routing_hint.py`** — single
  source of truth for both Plan B's planner hint (per-intent one-liner
  with "do NOT pick X" warnings) and Plan D's synthesizer cheat sheet
  (keyword → tool mapping across all 33 tools). Imported as a small
  module so future tweaks stay in one file.
- **Plan B — per-intent planner hint.** `_build_plan_prompt` now
  injects a `Routing hint: <intent-specific paragraph>` line right
  after `Detected intent:`. Each paragraph names the correct
  agent + tool, and includes a "do NOT pick" warning for the
  classifier failure mode the intent is most likely to mis-fire on:
  - `quote` → data_agent · `get_quote` (warns against get_history etc.)
  - `history` → data_agent · `get_history` with explicit `time_range`
    (warns against get_quote — the §0.4.12 regression)
  - `fundamentals` → data_agent · `get_fundamentals`
  - `news` → news_agent · `get_news`
  - `alpha` → alpha_agent · `compute_alpha_factors` (with ticker) /
    `list_alpha_factors` (catalogue)
  - `compare` → PTC mode with `get_quotes_batch` (warns against serial)
  - `analysis` → data_agent + news_agent; explicit opt-in for
    `run_trading_agents_analysis` only when the user said "跑一下"
  - CRUD intents (note/alert/watchlist/scheduled) → _CRUD_DISPATCH;
    planner told NOT to re-plan writes.
  - `report` / `run` → list_reports + get_report; list_runs +
    get_analysis_status + cancel_analysis_run.
  - `unknown` → recovery paragraph that walks the LLM through the
    keyword heuristic (ticker + N天 → history, ticker + 市盈率 →
    fundamentals, etc.) so ambiguous input still degrades gracefully.
- **Plan D — Tool Routing Cheat Sheet appended to `_SYNTH_SYSTEM`.**
  The synthesizer's system prompt now ends with a "Tool Routing
  Cheat Sheet" block listing every tool name grouped by category
  (行情数据 / CRUD 实体 / 分析与报告). SynthesizeNode reads it as a
  few-shot anchor when tool_results cover multiple tools or when
  intent was ambiguous at planning time. Names are byte-identical
  to the registered tool names so the citation block's
  `> 来源: <tool_name> (<symbol>)` format matches.

### Tests

- `tests/test_step46_intent_routing_hint.py` — 9 cases covering
  hint module coverage, str/Intent dual input, history-vs-quote
  regression, plan-prompt injection, unknown recovery, cheat-sheet
  completeness (all 33 tools present), synth system prompt
  integration, and `_PLAN_SYSTEM` byte-stability.

### Files

- new: `tradingagents/agent_harness/core/tool_routing_hint.py` (177 LoC)
- new: `tests/test_step46_intent_routing_hint.py` (206 LoC)
- changed: `tradingagents/agent_harness/core/orchestrator.py`
  - +5 lines import
  - +7 lines routing-hint injection in `_build_plan_prompt`
  - +5 lines cheat-sheet append in `_SYNTH_SYSTEM`
- changed: `pyproject.toml` (version bump 0.4.12 → 0.4.13)

## [0.4.12] — 2026-09-22

Harness tools: auto-normalise bare 6-digit A-share tickers, route history
queries to `get_history` instead of `get_quote`, fix provider signature
mismatch in the history tool.

### Fixed

- **`get_quote` / `get_history` accept bare 6-digit codes** like `513880`.
  New `_normalize_a_share_symbol()` prepends `.SS` (5/6/9xxxxx) or `.SZ`
  (0/2/3xxxxx) when the user types a plain A-share code. Stops the
  `no_data` returns that every user reported on first contact with a
  Chinese ETF or bank stock.
- **Tier-1 routing routes history queries to `get_history`**, not
  `get_quote`. The previous classifier fell through to QUOTE because
  `价格` substring-matched `_TIER1_KEYWORDS` and the user got a
  snapshot instead of candles. New `_TIER1_HISTORY_KEYWORDS` +
  `_TIER1_HISTORY_WINDOW_RE` (Chinese digit aware) win over QUOTE so
  "最近 30 天的价格走势", "过去一周行情", "history of NVDA" all reach
  the right tool. Existing QUOTE / NEWS / ANALYSIS / COMPARE / WATCHLIST
  paths unchanged.
- **`get_history` no longer crashes with `TypeError: takes 5 positional
  arguments but 6 were given`**. The provider chain's
  `get_candles(symbol, interval, start, end)` is 4-positional; the tool
  was passing a stray `asset_type` 5th arg. Dropped it — the supported
  asset type is `stock` only and adding it later is a one-line change
  to the tool when the providers grow `fund` / `crypto` history.

### Added

- `tests/test_normalize_a_share_symbol.py` — 23 cases (SH/SZ ETF,
  主板/创业板/科创板, suffixes, edge cases).
- `tests/test_step25_history_intent.py` — 8 HISTORY + 4 QUOTE cases.

### Files

- `tradingagents/agent_harness/tools/builtin.py` (`_normalize_a_share_symbol`,
  `get_quote` / `get_history` use it; tool descriptions mention
  auto-normalise and history vs quote split).
- `tradingagents/agent_harness/core/tier.py`
  (`_TIER1_HISTORY_KEYWORDS`, `_TIER1_HISTORY_WINDOW_RE`,
  `classify_intent` checks them before QUOTE).

## [0.4.11] — 2026-09-22

Harness sidebar: surface session list on collapsed rail so users who
accidentally collapse the sidebar (or land here with `sidebarExpanded`
= "0" from a previous Codex in-app browser session) can still find
their conversations.

### Added

- **Rail "展开会话列表" button** (`#harness-rail-expand`). Visible only
  when the sidebar is collapsed; clicking it expands the session list.
  Replaces the small 24×24 circular toggle as the primary discovery
  surface in the rail.
- **Rail "current session" caption** (`#harness-rail-active-title`).
  Vertical-text preview of the active session's title at the bottom
  of the rail so the user always knows which conversation is open
  even when the full sidebar is collapsed. Click it to expand.
- **Accent-coloured toggle in collapsed mode**. The existing
  `#harness-sidebar-toggle` is now rendered with the accent background
  + white text in collapsed mode so the rail surfaces a clear
  affordance to re-open the list.

### Files

- `web/static/harness.html` (rail markup)
- `web/static/agent.css` (collapsed-only visibility + accent toggle)
- `web/static/harness.js` (wire `#harness-rail-expand` click →
  `setSidebarExpanded(true)`; sync active title from
  `state.sessionId + state.sessions`)

## [0.4.7] — 2026-09-22

Harness L3 LLM-judge verification + orchestrator robustness.

### Added

- **L3 LLM-judge opt-in.** Set ``TRADINGAGENTS_ENABLE_L3=1`` in ``.env`` and
  restart; the verifier fires on every Tier 2 / Tier 3 turn whose plan
  produced ≥5 tool calls (``state.tool_results > 4``) and whose judge
  factory is configured (default uses the main LLM unless
  ``TRADINGAGENTS_JUDGE_PROVIDER`` / ``TRADINGAGENTS_JUDGE_MODEL`` are set).
  On an ungrounded verdict with ``claim_audit`` failures the orchestrator
  automatically triggers a single turn-level synth retry with a directive
  listing the unsupported numbers. Visible to the chat bubble via the new
  ``answer_verified`` SSE event (``{"level": 3, "score": 0.45, ...}``).
- **Bootstrap probe**: when 6+ watchlist symbols are quoted in one turn
  (PTC mode), the L3 path runs end-to-end — verified that the judge
  caught a real hallucination ("市场 cap 缩小 10x") in a 6-stock compare.

### Fixed

- **L3 replan blew up in PTC mode.** ``state.plan.append(...)`` raised
  ``AttributeError: 'dict' object has no attribute 'append'`` whenever
  the orchestrator used a PTC (parallel-tool-call) program (multi-symbol
  fan-out), which is exactly the workload that pushes ``tool_results > 4``
  and triggers L3. ``state.plan`` is ``list[dict] | dict``; for the dict
  (PTC) case, the replan intent is now stashed on
  ``state.replan_reason`` / ``state.replan_details`` so the post-synth
  retry path can still consume it.


## [0.4.6] — 2026-09-22

Harness reliability + invalid-symbol protection: orchestrator NameError fix,
``get_report`` payload capping, per-symbol quote circuit breaker, plus the
remaining ``§P3-5`` re-analysis plumbing.

### Fixed

- **Orchestrator ``_llm_plan`` referenced undefined locals.** The "all no-data"
  short-circuit branch read ``tool_results`` and ``base`` without defining
  them, so every such turn raised ``NameError`` and silently fell back to
  the heuristic planner. Alias to ``state.tool_results`` and initialise
  ``base = {}`` so the short-circuit returns cleanly and downstream
  reasoning sees the structured summary instead of an opaque heuristic plan.
- **``get_report`` tool dumped 400KB+ into a single SSE event.** The tool
  surfaced the full record (``analysts`` blobs, ``complete_report_html``,
  per-section ``*_html``) as the meta payload, which round-tripped through
  ``JSON.stringify + innerHTML`` on the chat bubble and froze the tab.
  Cap meta to a whitelist of summary fields (ticker / signal / status /
  models / based_on_report_id / …) and keep the 8KB content truncation but
  point users to ``/reports/<id>`` for the full detail.
- **Cooldown early-return leaked the singleflight slot.** When the new
  per-(symbol, asset_type) circuit breaker short-circuited, the inflight
  entry was never unregistered, so subsequent callers became waiters on a
  stale event and kept returning the cached cooldown snapshot forever.
  Wrap the cooldown branch in ``try / finally`` and call
  ``_unregister_inflight`` so the slot is released.

### Added

- **Per-symbol quote circuit breaker** in ``QuoteService``. After
  ``TRADINGAGENTS_QUOTE_CIRCUIT_THRESHOLD`` (default 3) failures within
  ``TRADINGAGENTS_QUOTE_CIRCUIT_WINDOW_SECONDS`` (default 60), the
  upstream chain is skipped for ``TRADINGAGENTS_QUOTE_CIRCUIT_COOLDOWN_SECONDS``
  (default 300). While open, ``get_quote`` returns a snapshot with
  ``cache_status="cooldown"`` / ``provider_status="cooldown"`` instead of
  timing out, so a single bad ticker (e.g. a stray ``TEST.SS``) can no
  longer wedge the prewarmer or the watchlist API. ``NOT_CONFIGURED`` is
  excluded so provider-disabled responses don't trip the breaker, and
  ``QuoteService.reset_circuit()`` is exposed for manual clearing.
- **QuoteSnapshot status literals extended** with ``"cooldown"`` for both
  ``cache_status`` and ``provider_status`` to surface the breaker state to
  the UI without breaking the Pydantic validators.

### Misc

- §P3-5 re-analysis routing plumbing: planner prompt adds the
  re-analysis pattern hint, command resolver accepts both ``dict`` and
  ``OrchestratorState``, tier detection picks up re-run verbs
  (``再分析`` / ``re-analyze`` / ``re-look`` / …) so
  ``基于 600036.SS 之前那份报告再分析一下`` reaches
  ``run_trading_agents_analysis`` with ``based_on_report_id`` set.
 to TradingAgents are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Breaking changes within the 0.x line are called out explicitly.

## [0.3.1] — 2026-07-05

Correctness and stability patch: data look-ahead, graph-router crash-safety,
checkpoint identity, crypto sentiment sources, and configurable resilience.

### Fixed

- **Alpha Vantage look-ahead filter now runs.** The fundamentals payload is a
  JSON string, so the dict-only guard skipped filtering and future-dated reports
  leaked into historical runs; parse before filtering. (#1115, @zachthebird)
- **News analyst prompt matches the tool.** The prompt advertised
  `get_news(query, ...)` but the tool takes a ticker; aligned to stop
  hallucinated free-text query calls. (#1116, @shcheuk)
- **Shared debate/risk routers can't crash mid-run.** Both routers return more
  targets than any one edge mapped; every edge now shares the complete path map,
  so a fall-through under prompt/i18n/refactor drift stays routable.
  (#1088, @Fr3ya, @sa7an7, @Sushanth012)
- **Checkpoint resume respects graph shape.** The thread id folds in selected
  analysts, debate/risk depth, and asset mode, so a resume under different
  choices no longer continues the wrong graph. (#1089, @bossjoker1, @Ghraven)
- **Crypto sentiment sources resolve.** StockTwits lists crypto as `<BASE>.X`
  (Yahoo's `BTC-USD` 404s) and Reddit needs the base symbol to match; the social
  path now maps crypto correctly for both. (#1113, @suremadoreai)

### Added

- **Configurable LLM retry budget.** `llm_max_retries` /
  `TRADINGAGENTS_LLM_MAX_RETRIES` is forwarded to every provider, so a transient
  429 burst no longer aborts a run. (#1091, @yanggaome)
- **Bedrock API-key auth.** `AWS_BEARER_TOKEN_BEDROCK` authenticates Amazon
  Bedrock without AWS access keys and takes precedence over an ambient
  `AWS_PROFILE`. (#1103, @praxstack)
- **Latest Claude models.** Added Claude Sonnet 5 (`claude-sonnet-5`) and
  Fable 5 (`claude-fable-5`); effort control now covers the Claude 5 line.

## [0.3.0] — 2026-06-22

Stabilization and extensibility release: a CI gate, a unified verified
data-access contract, a provider and data-vendor registry, and a maintenance
sweep that hardened config precedence, the model catalog, data resilience, and
structured output.

### Added

- **CI gate.** GitHub Actions runs the pytest suite across Python 3.10-3.13,
  strict `ruff`, and a clean-install smoke that imports the package and CLI to
  catch undeclared dependencies. (#994, #197)
- **Provider registry.** OpenAI-compatible providers register as a single spec,
  and a generic `openai_compatible` endpoint covers vLLM, LM Studio, and relays.
  Adds NVIDIA NIM, Kimi, Groq, Mistral, and a native Amazon Bedrock client.
- **Macro and prediction-market vendors.** FRED macro indicators and Polymarket
  event probabilities, surfaced to the news and macro analysts.
- **Programmatic report output.** `TradingAgentsGraph.save_reports()` writes the
  same report tree the CLI produces, for headless and API runs. (#1037)
- **Env-configurable reasoning depth** via `TRADINGAGENTS_OPENAI_REASONING_EFFORT`,
  `TRADINGAGENTS_GOOGLE_THINKING_LEVEL`, and `TRADINGAGENTS_ANTHROPIC_EFFORT`,
  each gated to the models that accept it.

### Changed

- **Verified data-access contract.** Symbol normalization on every vendor path
  (identity, returns, CLI, news); the configured vendor list is the exact
  resolution chain with no silent fallback to unselected vendors; a typed
  `VendorError` taxonomy; look-ahead-safe news windows; stale-OHLCV rejection;
  inclusive yfinance date ranges.
- **Config precedence.** An explicit `TRADINGAGENTS_*` value or CLI flag now wins
  over interactive defaults for debate and risk round counts,
  `--checkpoint / --no-checkpoint`, and the Docker provider profile; invalid
  boolean env values fail loudly. (#975, #976, #977)
- **Current-generation model catalog.** Refreshed provider lineups; retired
  `gpt-4.1`, Claude Sonnet 4.5, and the Gemini 2.5 line.
- **Optional vendors degrade** instead of aborting a run: a failed macro or
  prediction-market lookup returns a no-data sentinel.
- **Analyst prompts lead with the current date** so tool-call date ranges anchor
  to the run date rather than the model's training cutoff. (#836)

### Fixed

- **Instrument identity.** Deterministic ticker-to-company resolution prevents
  wrong-company hallucination, and a verified market-data snapshot grounds price
  and indicator claims. (#814, #830)
- **Social and market data sources.** Reddit RSS-first with 429 backoff,
  StockTwits transport hardening, and Alpha Vantage timeout plus
  key-versus-rate-limit handling.
- **Structured output.** Local OpenAI-compatible servers no longer reject
  object-form `tool_choice`; a thinking model that returns no parsed result falls
  back to free text; null-ish strings in optional price fields coerce to `None`.
  (#1038, #1051, #1057)

### Removed

- The no-op `analyst_concurrency_limit` config knob; parallel analyst execution
  is planned for a later release. (#979)
- The unused committed `uv.lock`. (#1030)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

[@CadeYu](https://github.com/CadeYu), [@Zavianx](https://github.com/Zavianx), [@weijianz-opc](https://github.com/weijianz-opc), [@naltun](https://github.com/naltun), [@brahmasky](https://github.com/brahmasky), [@nik2208](https://github.com/nik2208), [@thieucong98](https://github.com/thieucong98), [@Derekko-web](https://github.com/Derekko-web), [@LukiPrince](https://github.com/LukiPrince), [@Eddieargenal](https://github.com/Eddieargenal), [@Ghraven](https://github.com/Ghraven), [@ms32035](https://github.com/ms32035), [@yting27](https://github.com/yting27), [@nyxst4ck](https://github.com/nyxst4ck), [@KenCheung-AIxFinance](https://github.com/KenCheung-AIxFinance), [@yangyusheng2n](https://github.com/yangyusheng2n), [@fareloj](https://github.com/fareloj), [@haosenwang1018](https://github.com/haosenwang1018), [@octo-patch](https://github.com/octo-patch), [@seifenk](https://github.com/seifenk), [@CaoYuhaoCarl](https://github.com/CaoYuhaoCarl), [@mihailnica10](https://github.com/mihailnica10), [@Dado-hash](https://github.com/Dado-hash), [@Handsomemikezzz](https://github.com/Handsomemikezzz), [@ydhawesome](https://github.com/ydhawesome), [@macd2](https://github.com/macd2), [@AyushKar2005](https://github.com/AyushKar2005), [@wildhuman](https://github.com/wildhuman), [@robert23kim](https://github.com/robert23kim), [@bngness](https://github.com/bngness), [@tedix-rodrigo](https://github.com/tedix-rodrigo), [@malaccan](https://github.com/malaccan), [@rfalken78](https://github.com/rfalken78), [@dengli1971-droid](https://github.com/dengli1971-droid), [@proofconcept39](https://github.com/proofconcept39), [@prasta1](https://github.com/prasta1), [@liximin](https://github.com/liximin), [@jeffhuen](https://github.com/jeffhuen), [@mazar](https://github.com/mazar), [@soyangelromero](https://github.com/soyangelromero), [@CNQQC](https://github.com/CNQQC), [@dovetaill](https://github.com/dovetaill), [@fperdigon](https://github.com/fperdigon), [@gyx09212214-prog](https://github.com/gyx09212214-prog), [@RSXLX](https://github.com/RSXLX).

## [0.2.5] — 2026-05-11

### Added

- **Grounded Sentiment Analyst.** The renamed `sentiment_analyst` now reads
  real Yahoo News, StockTwits, and Reddit data before generating its report,
  replacing the prior flow that could fabricate social posts under prompt
  pressure. (#557, #607)
- **MiniMax provider** with the full M2.x catalog (M2.7 / M2.5 / M2.1 / M2
  plus highspeed variants, 204K context). Dual-region: Global
  (`MINIMAX_API_KEY`) and China (`MINIMAX_CN_API_KEY`).
- **Dual-region Qwen and GLM** with separate keys per region — international
  (`DASHSCOPE_API_KEY`, `ZHIPU_API_KEY`) and China (`DASHSCOPE_CN_API_KEY`,
  `ZHIPU_CN_API_KEY`), selectable via a secondary region prompt. (#758)
- **`TRADINGAGENTS_*` env-var configurability for `DEFAULT_CONFIG`.** Override
  `llm_provider`, deep/quick model IDs, `backend_url`, `output_language`,
  debate-round counts, checkpoint flag, and benchmark ticker via `.env` with
  type-aware coercion (string / int / bool). (#602)
- **Interactive API-key detection in the CLI.** When the selected provider's
  key is missing, the CLI prompts for it and persists the value to `.env`
  so the analysis run continues without restart.
- **Remote Ollama support.** `OLLAMA_BASE_URL` points the CLI and the
  programmatic client at a remote `ollama-serve`. The CLI surfaces the
  resolved endpoint and warns on common malformed inputs. Adds a
  `"Custom model ID"` option for models pulled via `ollama pull`. (#648, #768)
- **Configurable news-fetch parameters** in `DEFAULT_CONFIG` — per-ticker
  article limit, macro headline limit, lookback window, and macro search
  queries. (#606, #683)
- **Configurable alpha benchmark** for non-US tickers. Replaces hardcoded
  SPY with regional indices for `.NS` (^NSEI), `.T` (^N225), `.HK` (^HSI),
  `.L` (^FTSE), `.TO` (^GSPTSE), `.AX` (^AXJO), `.BO` (^BSESN); explicit
  `benchmark_ticker` override available. Eliminates FX drift dominating
  alpha for non-USD listings. (#628, #684)
- **Multi-language output covers every user-facing agent** — researchers,
  risk debators, research manager, and trader, ending the previous
  partial-localization reports. (#575)
- **Model catalog refresh.** OpenAI GPT-5.5 frontier, Anthropic Claude Opus
  4.7, Gemini 3.1 Flash-Lite GA, xAI Grok 4.20, Qwen 3.6 line. Versioned IDs
  only; auto-shifting aliases moved to the `"Custom model ID"` option.

### Changed

- **Sentiment Analyst** is now consistently named across the CLI dropdown,
  status panel, and final reports (previously the backend was renamed but
  the CLI still said "Social Analyst"). The `AnalystType.SOCIAL = "social"`
  wire value is kept for saved-config back-compat.

### Fixed

- **Structured output works on DeepSeek V4 / reasoner and MiniMax M2.x.**
  Those providers reject `tool_choice` per their tool-calling docs; the
  binding flow now skips it automatically via a capability table.
- **`pip install .` installations pick up the project `.env`** when running
  the CLI as a console script. (#747)
- **Reports save end-to-end** — streamed chunks were previously dropped from
  `complete_report.md`. (#719, #736)
- **Ticker prompt preserves exchange suffixes** (`.SH`, `.SZ`, `.SS`, `.HK`,
  `.T`, etc.) for A-share, HK, Tokyo, and other non-US flows. (#770)
- **Docker permission errors** no longer block first-run write to
  `~/.tradingagents/`. (#519, #627, #672, #771)
- **Config state no longer leaks between runs** when sub-dicts are mutated;
  `set_config` partial updates preserve sibling defaults. (#788)
- **`max_recur_limit` config actually applies** — previously read but not
  forwarded to the propagator. (#764)
- **Missing-API-key error** names the exact env var to set. (#680)
- **Quieter startup** — suppressed the noisy upstream
  `LangChainPendingDeprecationWarning` from langgraph-checkpoint; will be
  removed once that package ships its fix.

### Security

- **Ticker path-traversal validation** at every filesystem-path site (cache,
  checkpoint database, results) so a malicious ticker cannot escape its
  intended directory. (#618)

## [0.2.4] — 2026-04-25

### Added

- **Structured-output decision agents.** Research Manager, Trader, and Portfolio
  Manager now use `llm.with_structured_output(Schema)` on their primary call
  and return typed Pydantic instances. Each provider's native structured-output
  mode is used (`json_schema` for OpenAI / xAI, `response_schema` for Gemini,
  tool-use for Anthropic, function-calling for OpenAI-compatible providers).
  Render helpers preserve the existing markdown shape so memory log, CLI
  display, and saved reports keep working unchanged. (#434)
- **LangGraph checkpoint resume** — opt-in via `--checkpoint`. State is saved
  after each node so crashed or interrupted runs resume from the last
  successful step. Per-ticker SQLite databases under
  `~/.tradingagents/cache/checkpoints/`. `--clear-checkpoints` resets them. (#594)
- **Persistent decision log** replacing the per-agent BM25 memory. Decisions
  are stored automatically at the end of `propagate()`; the next same-ticker
  run resolves prior pending entries with realised return, alpha vs SPY, and
  a one-paragraph reflection. Override path with `TRADINGAGENTS_MEMORY_LOG_PATH`.
  Optional `memory_log_max_entries` config caps resolved entries; pending
  entries are never pruned. (#578, #563, #564, #579)
- **DeepSeek, Qwen (Alibaba DashScope), GLM (Zhipu), and Azure OpenAI**
  providers, plus dynamic OpenRouter model selection.
- **Docker support** — multi-stage build with separate dev and runtime images.
- **`scripts/smoke_structured_output.py`** — diagnostic that exercises the
  three structured-output agents against any provider so contributors can
  verify their setup with one command.
- **5-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell) used
  consistently by Research Manager, Portfolio Manager, signal processor, and
  the memory log; Trader keeps 3-tier (Buy / Hold / Sell) since transaction
  direction is naturally ternary.
- **Pytest fixtures** — lazy LLM client imports plus placeholder API keys so
  the test suite runs cleanly without credentials. (#588)

### Changed

- **`backend_url` default is now `None`** rather than the OpenAI URL. Each
  provider client falls back to its native default. The previous default
  leaked the OpenAI URL into non-OpenAI clients (e.g. Gemini), producing
  malformed request URLs for Python users who switched providers without
  overriding `backend_url`. The CLI flow is unaffected.
- All file I/O passes explicit `encoding="utf-8"` so Windows users no longer
  hit `UnicodeEncodeError` with the cp1252 default. (#543, #550, #576)
- Cache and log directories moved to `~/.tradingagents/` to resolve Docker
  permission issues. (#519)
- `SignalProcessor` reads the rating from the Portfolio Manager's rendered
  markdown via a deterministic heuristic — no extra LLM call.
- OpenAI structured-output calls default to `method="function_calling"` to
  avoid noisy `PydanticSerializationUnexpectedValue` warnings emitted by
  langchain-openai's Responses-API parse path. Same typed result, no warnings.

### Fixed

- Empty memory no longer triggers fabricated past-lessons in agent prompts;
  the memory-log redesign makes this structurally impossible since only the
  Portfolio Manager consults memory and only when entries exist. (#572)
- Tool-call logging processes every chunk message, not just the last one, and
  memory score normalization handles empty score arrays. (#534, #531)

### Removed

- `FinancialSituationMemory` (the per-agent BM25 system) and the dead
  `reflect_and_remember()` plumbing; subsumed by the persistent decision log.
- Hardcoded Google endpoint that caused 404 when `langchain-google-genai`
  changed its API path. (#493, #496)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

- [@claytonbrown](https://github.com/claytonbrown) — checkpoint resume (#594), test fixtures (#588), design feedback on cost tracking (#582) and structured validation (#583)
- [@Bcardo](https://github.com/Bcardo) — memory-log redesign (#579), empty-memory hallucination report (#572), encoding fix proposal (#570)
- [@voidborne-d](https://github.com/voidborne-d) — memory persistence design (#564), portfolio manager state fix (#503)
- [@mannubaveja007](https://github.com/mannubaveja007) — structured-output feature request (#434)
- [@kelder66](https://github.com/kelder66) — RAM-only memory issue (#563)
- [@Gujiassh](https://github.com/Gujiassh) — tool-call logging fix (#534), test stub PR (#533)
- [@iuyup](https://github.com/iuyup) — memory score normalization fix (#531)
- [@kaihg](https://github.com/kaihg) — Google base_url fix (#496)
- [@32ryh98yfe](https://github.com/32ryh98yfe) — Gemini 404 report (#493)
- [@uppb](https://github.com/uppb) — OpenRouter dynamic model selection (#482)
- [@guoz14](https://github.com/guoz14) — OpenRouter limited-model report (#337)
- [@samchenku](https://github.com/samchenku) — indicator name normalization (#490)
- [@JasonOA888](https://github.com/JasonOA888) — y_finance pandas import fix (#488)
- [@tiffanychum](https://github.com/tiffanychum) — stale import cleanup (#499)
- [@zaizou](https://github.com/zaizou) — Docker permission issue (#519)
- [@Stosman123](https://github.com/Stosman123), [@mauropuga](https://github.com/mauropuga), [@hotwind2015](https://github.com/hotwind2015) — Windows encoding bug reports (#543, #550, #576)
- [@nnishad](https://github.com/nnishad), [@atharvajoshi01](https://github.com/atharvajoshi01) — encoding fix proposals (#568, #549)

## [0.2.3] — 2026-03-29

### Added

- **Multi-language output** for analyst reports and final decisions, with a
  CLI selector. Internal agent debate stays in English for reasoning quality. (#472)
- **GPT-5.4 family models** in the default catalog, with deep/quick model split.
- **Unified model catalog** as a single source of truth for CLI options and
  provider validation.

### Changed

- `base_url` is forwarded to Google and Anthropic clients so corporate proxies
  work consistently across providers. (#427)
- Standardised the Google `api_key` parameter to the unified `api_key` form.

### Fixed

- Backtesting fetchers no longer leak look-ahead data when `curr_date` is in
  the middle of a fetched window. (#475)
- Invalid indicator names from the LLM are caught at the tool boundary instead
  of crashing the run. (#429)
- yfinance news fetchers respect the same exponential-backoff retry as price
  fetchers. (#445)

### Contributors

- [@ahmedk20](https://github.com/ahmedk20) — multi-language output (#472)
- [@CadeYu](https://github.com/CadeYu) — model catalog typing (#464)
- [@javierdejesusda](https://github.com/javierdejesusda) — unified Google API key parameter (#453)
- [@voidborne-d](https://github.com/voidborne-d) — yfinance news retry (#445)
- [@kostakost2](https://github.com/kostakost2) — look-ahead bias report (#475)
- [@lu-zhengda](https://github.com/lu-zhengda) — proxy/base_url support request (#427)
- [@VamsiKrishna2021](https://github.com/VamsiKrishna2021) — invalid indicator crash report (#429)

## [0.2.2] — 2026-03-22

### Added

- **Five-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell)
  introduced for the Portfolio Manager.
- **Anthropic effort level** support for Claude models.
- **OpenAI Responses API** path for native OpenAI models.

### Changed

- `risk_manager` renamed to `portfolio_manager` to match the role description
  shown in the CLI display.
- Exchange-qualified tickers (e.g. `7203.T`, `BRK.B`) preserved across all
  agent prompts and tool calls.
- Process-level UTF-8 default attempted for cross-platform consistency
  (note: this approach did not actually take effect; replaced in v0.2.4 with
  explicit per-call `encoding="utf-8"` arguments).

### Fixed

- yfinance rate-limit errors are retried with exponential backoff. (#426)
- HTTP client SSL customisation is supported for environments that need
  custom certificate bundles. (#379)
- Report-section writes handle list-of-string content gracefully.

### Contributors

- [@CadeYu](https://github.com/CadeYu) — exchange-qualified ticker preservation (#413)
- [@yang1002378395-cmyk](https://github.com/yang1002378395-cmyk) — HTTP client SSL customisation (#379)

## [0.2.1] — 2026-03-15

### Security

- Patched `langchain-core` vulnerability (LangGrinch). (#335)
- Removed `chainlit` dependency affected by CVE-2026-22218.

### Added

- `pyproject.toml` build-system configuration; the project now installs via
  modern packaging tooling.

### Removed

- `setup.py` — dependencies consolidated to `pyproject.toml`.

### Fixed

- Risk manager reads the correct fundamental report source. (#341)
- All `open()` calls receive an explicit UTF-8 encoding (initial pass).
- `get_indicators` tool handles comma-separated indicator names from the LLM. (#368)
- `Propagation` initialises every debate-state field so risk debaters never
  see missing keys.
- Stock data parsing tolerates malformed CSVs and NaN values.
- Conditional debate logic respects the configured round count. (#361)

### Contributors

- [@RinZ27](https://github.com/RinZ27) — `langchain-core` security patch (#335)
- [@Ljx-007](https://github.com/Ljx-007) — risk manager fundamental-report fix (#341)
- [@makk9](https://github.com/makk9) — debate-rounds config issue (#361)

## [0.2.0] — 2026-02-04

This is the largest release since the initial public version. The framework
moved from single-provider to a multi-provider architecture and grew several
production-ready surfaces.

### Added

- **Multi-provider LLM support** (OpenAI, Google, Anthropic, xAI, OpenRouter,
  Ollama) via a factory pattern, with provider-specific thinking configurations.
- **Alpha Vantage** integration as a configurable primary data provider, with
  yfinance as a community-stability fallback.
- **Footer statistics** in the CLI: real-time tracking of LLM calls, tool
  calls, and token usage via LangChain callbacks.
- **Post-analysis report saving** — the framework writes per-section markdown
  files (analyst reports, debate transcripts, final decision) when a run
  completes.
- **Announcements panel** — fetches updates from `api.tauric.ai/v1/announcements`
  for the CLI welcome screen.
- **Tool fallbacks** so a single vendor outage does not stop the pipeline.

### Changed

- Risky / Safe risk debaters renamed to **Aggressive / Conservative** for
  consistency with the displayed agent labels.
- Default data vendor switched to balance reliability and quota across
  community deployments.
- Ollama and OpenRouter model lists updated; default endpoints clarified.

### Fixed

- Analyst status tracking and message deduplication in the live display.
- Infinite-loop guard in the agent loop; reflection and logging hardened.
- Various data-vendor implementation bugs and tool-signature mismatches.

### Contributors

This release is the first with substantial outside contributions; many community
PRs from late 2025 also landed here.

- [@luohy15](https://github.com/luohy15) — Alpha Vantage data-vendor integration (#235)
- [@EdwardoSunny](https://github.com/EdwardoSunny) — yfinance fetching optimisations (#245)
- [@Mirza-Samad-Ahmed-Baig](https://github.com/Mirza-Samad-Ahmed-Baig) — infinite-loop guard, reflection, and logging fixes (#89)
- [@ZeroAct](https://github.com/ZeroAct) — saved results path support (#29)
- [@Zhongyi-Lu](https://github.com/Zhongyi-Lu) — `.env` gitignore (#49)
- [@csoboy](https://github.com/csoboy) — local Ollama setup (#53)
- [@chauhang](https://github.com/chauhang) — initial Docker support attempt (#47, later reverted; the merged Docker support shipped in v0.2.4)

## [0.1.1] — 2025-06-07

### Removed

- Static site assets that had been bundled with v0.1.0; the public site now
  lives separately.

## [0.1.0] — 2025-06-05

### Added

- **Initial public release** of the TradingAgents multi-agent trading
  framework: market / sentiment / news / fundamentals analysts; bull and bear
  researchers; trader; aggressive, conservative, and neutral risk debaters;
  portfolio manager. LangGraph orchestration, yfinance data, per-agent
  BM25 memory, single-provider OpenAI integration, interactive CLI.

[0.2.4]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/TauricResearch/TradingAgents/releases/tag/v0.1.0

## [0.4.8] — 2026-09-22

Fix planner slot override so multi-symbol compare / quote queries route
to PTC with N parallel `get_quote` calls instead of being hijacked by
the (RUN, CREATE) override into a single `run_trading_agents_analysis`
call. Restores L3 trigger (L3 requires `len(tool_results) > 4`).

### Fixed

- `tradingagents/agent_harness/core/tier.py:extract_slots` (§Step 23) —
  compare-style messages (multi-symbol **or** explicit compare verb)
  no longer set `trade_date` from bare "今天 / 今日 / today" or bare-ISO
  matches, so the slot-aware override at `classify()` no longer flips
  the intent to (RUN, CREATE). Six-stock compare queries now dispatch
  6 parallel `get_quote` calls via the planner's PTC mode, restoring
  L3 firing conditions.
- `tradingagents/agent_harness/core/tier.py:_ENTITY_KW[Intent.SCHEDULED]`
  (§Step 23) — removed bare "收盘" / "开盘" keywords that substring-matched
  "收盘价" / "开盘价" (closing/opening price) and hijacked every quote
  flow to `list_scheduled_tasks`. Compound forms ("收盘后" / "盘后" /
  "盘后跑" / etc.) are unambiguous and kept.

### Tests

- `tests/test_step22_trade_date_analysis_gate.py` — 6 new cases covering
  the §Step 23 compare-style guards (multi-symbol today / multi-symbol
  no-verb / compare-verb single-symbol / multi-symbol ISO date /
  analysis-multi-symbol still extracts / end-to-end routing).

## [0.4.9] — 2026-09-22

Two L3 grounding bugs fixed: planner-agent scope wiring was silently
dropped for the monkey-patched `PlannerAgent` and the hand-written
`VerifierAgent`, and `claim_audit` lost every tool-derived number
because the haystack walker didn't recognise Pydantic BaseModel
results (e.g. `QuoteResult` from `get_quote`).

### Fixed

- `tradingagents/agent_harness/agents/planner.py:_patched_init`
  (§Step 24) — declare `llm_factory` / `tool_registry` / `scope`
  explicitly instead of relying on a bare `**kwargs` catch-all.
  `SubagentProvider.build` filters kwargs against
  `inspect.signature(factory).parameters`, and `**kwargs` shows up
  in `sig.parameters` under the name `kwargs` — so any explicit
  kwarg name (e.g. `scope`) was silently dropped, leaving
  `planner.scope = None`. Caught by
  `test_harness_wires_scope_into_agents`.
- `tradingagents/agent_harness/agents/verifier.py:VerifierAgent.__init__`
  (§Step 24) — added explicit `scope` kwarg and forwarded it to
  `super().__init__()`; same root cause as the planner fix.
- `tradingagents/agent_harness/verification/claim_audit.py:_walk`
  (§Step 24 P2) — recurse into Pydantic v2 models via
  `model_dump()` (and v1 via `.dict()`), and fall back to
  `vars(value)` for non-Pydantic structured objects. Previously a
  `QuoteResult(BaseModel)` stored in `state.tool_results[i]["result"]`
  was silently dropped from the haystack, leaving every number in
  the answer reported as unsupported and `claim_audit_score = 0.0`.
  Production impact: L3 was always returned `ungrounded` because
  claim_audit dragged the score down even when the LLM judge
  confirmed the numbers came from tool data.
- `tradingagents/agent_harness/verification/claim_audit.py:_number_in_haystack`
  (§Step 24 P1) — strip leading `+` from the needle so `+0.22%`
  matches `0.22` in tool data, and tokenise multi-value haystack
  strings (e.g. `repr(QuoteResult)` blobs) with `_NUMBER_RE` so
  each numeric value is compared individually instead of feeding
  the whole blob to `_to_float` (which returned None and skipped
  the float comparison path).

### Tests

- `tests/test_step30_claim_audit.py` — 6 new cases: leading-plus
  sign, multi-value haystack tokenisation, full 6-stock compare
  on stringified blobs, fabricated-number negative regression, and
  2 Pydantic `QuoteResult` end-to-end cases.
- `tests/test_agent_scope.py` — already had
  `test_harness_wires_scope_into_agents`; was pre-existing red on
  main, now green.

## [0.4.10] — 2026-09-22

Frontend render fix for stringified Pydantic tool results + cache
version bump so the user's browser picks up the new JS.

### Fixed

- `web/static/harness.js:appendToolResult` (§Step 24 P3) — when the
  SSE `default=str` fallback stringifies a non-JSON-native result
  (e.g. Pydantic `QuoteResult`), `payload.result` arrives as the
  Python `repr(...)` string. The previous generic-safety branch did
  `Object.keys(result).forEach(...)`, which on a string returns
  character indices and produced the bug
  ``📥 get_quote: {"0":"s","1":"y","2":"m","3":"b",...}``. Now
  string results short-circuit to a small helper that surfaces
  `symbol='...' · key=value, key=value...` instead.
- `web/static/index.html` / `web/static/harness.html` — bumped the
  `harness.js?v=` cache key from `20260917-harness-9` to
  `20260922-harness-l3` so existing browser caches pick up the new
  JS without manual hard-refresh.
