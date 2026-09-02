# TradingAgentsPlus 平台可靠性与可观测性实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复代码质量基线，并为 Web 平台增加可靠的长任务状态、SQLite 报告索引分页、行情源健康度和统一中文资源。

**Architecture:** 保留 FastAPI、原生 JavaScript、SQLite 和文件报告正文。先增加幂等 v2 migration 和专用 Repository，再让 RunManager、ReportHistory、QuoteService 与前端通过稳定契约使用这些边界；旧 `/api/history` list、既有 freshness/status 字段和中文固定界面保持兼容。

**Tech Stack:** Python 3.10+、FastAPI、Pydantic v2、SQLite、vanilla JavaScript、pytest、Node test、Ruff、Playwright。

**Spec:** `docs/superpowers/specs/2026-08-31-platform-reliability-operations-design.md`

---

## Chunk 1: Quality Baseline and Warning Cleanup

### Task 1: Make Ruff and compile checks clean

**Files:**
- Modify: `tests/test_chunk3_contract_fixes.py`
- Modify: `tests/test_web_repositories.py`
- Modify: `tests/test_web_snapshots.py`
- Modify: `tests/test_web_storage.py`
- Modify: `web/history.py`
- Modify: `web/app.py`
- Modify: `web/markdown.py`
- Modify: `web/market_localization.py`
- Modify: `web/models.py`
- Modify: `web/repositories.py`
- Modify: `web/storage.py`
- Modify: `cli/main.py`
- Modify: `tests/test_web_api.py`

- [ ] **Step 1: Record the failing lint baseline**

  Run: `ruff check .`

  Expected: FAIL with the current import-order, unused-variable, one-line-statement and typing-import violations.

- [ ] **Step 2: Apply mechanical Ruff-safe fixes**

  Use `ruff check . --fix` only for safe fixes, then manually expand one-line `if`/`try` statements in `web/repositories.py`, remove unused variables/imports, import `Iterator` from `collections.abc`, simplify the boolean return in `web/history.py`, and remove the duplicate `latest_content = content` assignment in `cli/main.py`. Do not alter public behavior.

- [ ] **Step 3: Replace the deprecated Starlette status constant**

  In `web/app.py`, replace `status.HTTP_422_UNPROCESSABLE_ENTITY` with `status.HTTP_422_UNPROCESSABLE_CONTENT`. Add a focused API test executed with `-W error::starlette.exceptions.StarletteDeprecationWarning` so the global pytest warning filter cannot hide a project-owned warning.

- [ ] **Step 4: Verify the quality baseline**

  Run:

  ```bash
  ruff check .
  python -m compileall -q tradingagents cli web
  pytest -q -W 'error::starlette.exceptions.StarletteDeprecationWarning' tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_api.py
  ```

  Expected: all commands pass with no project-owned deprecation warning.

- [ ] **Step 5: Commit quality cleanup**

  ```bash
  git add cli web tests
  git commit -m "chore: clean quality baseline"
  ```

## Chunk 2: Run Heartbeat, Deadline, and Terminal Integrity

### Task 2: Add v2 run lifecycle persistence

**Files:**
- Create: `web/migrations/002_reliability_operations.sql`
- Modify: `web/storage.py`
- Modify: `web/models.py`
- Modify: `web/repositories.py`
- Modify: `web/config.py`
- Modify: `web/app.py`
- Modify: `tradingagents/default_config.py`
- Test: `tests/test_web_storage.py`
- Test: `tests/test_web_models.py`
- Test: `tests/test_web_repositories.py`
- Test: `tests/test_web_api.py`

- [ ] **Step 1: Write failing migration/model tests**

  Add tests proving:

  - an existing v1 database gains `last_heartbeat_at`, `timeout_at`, `terminal_reason`, and the three resolved timeout columns;
  - old rows remain readable with NULL lifecycle fields;
  - a migration failure leaves `schema_version` and prior tables unchanged;
  - `RunStatus.TIMED_OUT`, `EventName.RUN_TIMED_OUT`, `RunTimedOutPayload`, and the new `RunRecord` fields serialize correctly;
  - v2 also creates the complete `reports`, `report_index_outbox`, and `provider_health` schemas from the approved spec, even though later tasks do not use them yet;
  - default timeout values are 7200/15/180 seconds;
  - environment keys are `TRADINGAGENTS_RUN_TIMEOUT_SECONDS`, `TRADINGAGENTS_RUN_HEARTBEAT_INTERVAL_SECONDS`, and `TRADINGAGENTS_RUN_HEARTBEAT_TIMEOUT_SECONDS`;
  - precedence is environment > SQLite > `DEFAULT_CONFIG` > hard fallback;
  - values outside the approved ranges are rejected with a configuration error rather than clamped;
  - `AnalysisRequest` still rejects all three timeout fields because it remains `extra="forbid"`.

- [ ] **Step 2: Run focused tests and verify RED**

  Run: `pytest -q tests/test_web_storage.py tests/test_web_models.py tests/test_web_repositories.py tests/test_web_api.py`

  Expected: FAIL because migration v2 and lifecycle models do not exist.

- [ ] **Step 3: Implement migration v2 and idempotent compatibility helpers**

  Add the final, complete v2 SQL for `reports`, `report_index_outbox`, and `provider_health`. Add a version-2 Python migration hook called from `_migrate()` after `BEGIN IMMEDIATE` and before SQL execution/version update. The hook receives the same connection, checks `PRAGMA table_info(web_runs)`, and conditionally adds every lifecycle column. Hook, SQL, and schema-version update therefore commit or roll back together. No later task may edit migration v2.

- [ ] **Step 4: Extend models and repository projections**

  Add:

  ```python
  class RunStatus(str, Enum):
      TIMED_OUT = "timed_out"

  class EventName(str, Enum):
      RUN_TIMED_OUT = "run_timed_out"
  ```

  Extend `RunRecord`, run snapshot payload, repository upsert/load, and persisted JSON with heartbeat/deadline/reason/config fields. Preserve `error_code` as the compatibility alias for terminal reasons.

- [ ] **Step 5: Implement timeout configuration resolution**

  Add the three defaults and env mappings in `tradingagents/default_config.py`, add the same keys to `SettingsRepository.ALLOWED`, and add `resolve_run_lifecycle_config(config, settings)` in `web/config.py`. It must parse integers, reject invalid/range-violating values, and return the effective values plus source metadata. The exact ranges are 300..86400 seconds for maximum run time, 5..60 seconds for heartbeat interval, and 30..600 seconds for heartbeat timeout. `create_app()` resolves these values once and passes them to `RunManager`; `start_run()` persists the resolved values and fixed `timeout_at` on each record. API integration tests prove an injected manager receives the resolved settings and invalid configuration fails application startup clearly.

- [ ] **Step 6: Verify migration/model tests GREEN**

  Run: `pytest -q tests/test_web_storage.py tests/test_web_models.py tests/test_web_repositories.py tests/test_web_api.py`

  Expected: all focused tests pass.

- [ ] **Step 7: Commit the complete v2 storage contract**

  ```bash
  git add web/migrations/002_reliability_operations.sql web/storage.py web/models.py web/repositories.py web/config.py web/app.py tradingagents/default_config.py tests/test_web_storage.py tests/test_web_models.py tests/test_web_repositories.py tests/test_web_api.py
  git commit -m "feat: add reliability storage schema"
  ```

### Task 3: Implement watchdog, heartbeats, monotonic progress, and recovery

**Files:**
- Modify: `web/manager.py`
- Modify: `web/runner.py`
- Modify: `web/app.py`
- Modify: `web/static/app.js`
- Modify: `tradingagents/graph/trading_graph.py`
- Modify: `tradingagents/llm_clients/openai_client.py`
- Modify: `tradingagents/llm_clients/anthropic_client.py`
- Modify: `tradingagents/llm_clients/google_client.py`
- Modify: `tradingagents/llm_clients/azure_client.py`
- Modify: `tradingagents/llm_clients/bedrock_client.py`
- Test: `tests/test_llm_timeout.py`
- Test: `tests/test_web_manager.py`
- Test: `tests/test_web_runner.py`
- Test: `tests/test_web_api.py`
- Test: `tests/test_web_static.py`

- [ ] **Step 1: Write failing manager lifecycle tests**

  Add fake-clock tests for:

  - heartbeat updates only queued/running records;
  - progress clamps to `[0,1]` and never decreases;
  - wall-clock deadline and heartbeat lease each CAS a run to `timed_out` exactly once;
  - late worker completion cannot replace `timed_out`;
  - terminal methods clear `current_agent` and completed forces progress 1;
  - queued/running restart becomes interrupted;
  - publishing restart completes only when the file gate is valid, otherwise becomes failed;
  - publishing is excluded from both heartbeat and wall-clock watchdog transitions;
  - normal `complete_publishing` with an incomplete file gate becomes `failed/publish_incomplete`;
  - invalid publishing output is moved to the controlled orphan/quarantine directory;
  - run row plus terminal event are written atomically or repaired on startup;
  - every terminal state sets canonical `terminal_reason`, preserves compatibility `error_code`, clears current agent, and preserves non-completed progress;
  - `get_run()`, Worker heartbeat writes, and watchdog checks first call the same deadline/lease CAS so a read or heartbeat cannot revive or expose an already-expired task;
  - shutdown stops the watchdog thread.

- [ ] **Step 2: Write failing SSE/API/client tests**

  Assert `RunRecord` exposes heartbeat/deadline/reason, `run_timed_out` has the documented payload, a synthetic `run_snapshot` SSE block has no `id:` line, and its payload contains `snapshot_seq` plus `replay_from_seq=snapshot_seq+1`. The browser must set `lastSeq=snapshot_seq` before replay. The polling fallback treats every status outside `queued/running/publishing` as terminal, including unknown future terminal values.

- [ ] **Step 3: Run lifecycle tests and verify RED**

  Run:

  ```bash
  pytest -q tests/test_llm_timeout.py tests/test_web_manager.py tests/test_web_runner.py tests/test_web_api.py tests/test_web_static.py
  ```

  Expected: lifecycle and timeout assertions fail.

- [ ] **Step 4: Implement lifecycle CAS and watchdog**

  Add one locked transition helper that:

  ```python
  def _transition_terminal(run_id, allowed, status, event, payload):
      # compare current status, persist run+event in one transaction,
      # clear current_agent, release active_run_id, notify subscribers
  ```

  Start a daemon watchdog with a stoppable event. Check `timeout_at` and heartbeat lease every configured interval, but only CAS `queued/running`; `publishing` is excluded from both wall-clock and heartbeat watchdog transitions and is repaired only during startup recovery. Factor expiry evaluation into one locked helper and call it from `get_run()`, heartbeat writes, and the watchdog before any normal state mutation. All worker completion/publication calls must reject terminal records. Register manager/watchdog shutdown through FastAPI lifespan so tests and production stop the thread deterministically.

- [ ] **Step 5: Add worker heartbeat and remaining-deadline propagation**

  Add a Worker-owned `maybe_touch_heartbeat()` checkpoint. The same Worker thread calls it at start, before and after every external Provider/LLM call, while consuming graph chunks, and on phase/activity/agent updates; it persists only when the configured interval elapsed, so there is no independent fake-heartbeat thread. A blocked call therefore stops heartbeats and remains bounded by the wall-clock deadline.

  Propagate a Web-only deadline supplier, not a timeout value calculated once. Immediately before every Provider or LLM request, a shared adapter computes `max(0, timeout_at-now)` and uses the smaller of that value and the provider's normal request cap. `TradingAgentsGraph` passes the supplier through its LLM wrappers; normalized OpenAI, Anthropic, Google, Azure and Bedrock invocation adapters resolve it per `invoke`, while CLI/programmatic configs without the supplier retain existing defaults. The dataflow vendor router uses the same supplier for yfinance/HTTP-backed tools so each external call is bounded by the then-current remaining time. Constructor/invocation tests cover all five LLM families plus at least one data Provider call. A timed-out Worker returning later must observe the terminal CAS and skip report publication.

- [ ] **Step 6: Implement consistent SSE snapshots and browser terminal mapping**

  Build the run snapshot and sequence cut under the manager lock. Emit a synthetic snapshot without a conflicting SSE id, then replay only events after `snapshot_seq`. Add `timed_out` to all client terminal-state sets and translate it as `分析超时`.

- [ ] **Step 7: Run lifecycle tests GREEN**

  Run:

  ```bash
  pytest -q tests/test_llm_timeout.py tests/test_web_manager.py tests/test_web_runner.py tests/test_web_api.py tests/test_web_static.py
  ```

  Expected: all lifecycle tests pass.

- [ ] **Step 8: Commit task reliability**

  ```bash
  git add web tradingagents/default_config.py tradingagents/graph/trading_graph.py tradingagents/llm_clients tradingagents/dataflows tests
  git commit -m "feat: harden analysis task lifecycle"
  ```

## Chunk 3: SQLite Report Index and Server Pagination

### Task 4: Add report index, outbox, and rebuild path

**Files:**
- Modify: `web/repositories.py`
- Modify: `web/history.py`
- Modify: `web/runner.py`
- Modify: `web/app.py`
- Test: `tests/test_web_storage.py`
- Test: `tests/test_web_repositories.py`
- Test: `tests/test_web_history.py`
- Test: `tests/test_web_runner.py`
- Test: `tests/test_web_api.py`

- [ ] **Step 1: Write failing report-index tests**

  Cover:

  - reports/provider metadata and legacy nullable fields survive round-trip;
  - only allowlisted `root_name` and safe relative paths are accepted;
  - `path_state=missing/unsafe` records are excluded;
  - completed publish upserts an index row;
  - forced index-upsert failure writes `report_index_outbox` and the overlay keeps the new report visible;
  - immediately after an outbox-backed publish, list, detail, and Markdown download all resolve the report before the run can emit `run_completed`;
  - retry removes outbox after successful upsert;
  - startup rebuild imports legacy/web reports with stable IDs and no duplicates;
  - pre-index Web reports with `run.json.status=completed` and `complete_report.md` but no historical `COMMITTED` marker remain readable as compatibility records, while every newly published/recovered run still requires the full three-file gate;
  - deleting a file changes path state on rebuild without scanning each API request.

- [ ] **Step 2: Run focused report tests and verify RED**

  Run: `pytest -q tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_history.py tests/test_web_runner.py tests/test_web_api.py`

  Expected: FAIL because report index APIs do not exist.

- [ ] **Step 3: Implement ReportIndexRepository**

  Do not modify migration v2 in this task; consume the tables created by Task 2. Add explicit methods:

  ```python
  upsert(metadata)
  enqueue(metadata, error)
  retry_outbox(limit=50)
  rebuild(roots)
  list_legacy_shape()
  search(page, page_size, filters, sort)
  get(report_id)
  ```

  Parameterize SQL, enforce root/path allowlists, normalize `rating/signal`, serialize list fields as JSON, and merge outbox rows by report ID before filtering/sorting/counting.
  Validate the approved allowed values, truncate `decision_preview` to 512 Unicode characters, sort NULL `generated_at` last/oldest, and always use `report_id` as the stable tie-breaker.

- [ ] **Step 4: Make ReportHistory an indexed read model**

  Keep Markdown/file detail reading and path safety in `ReportHistory`, but delegate list metadata to the index. Startup calls a bounded rebuild; detail lookup validates the indexed path and report gate. Preserve legacy list item fields exactly.

- [ ] **Step 5: Integrate publication and outbox retry**

  Required publication order: commit/rename report files -> upsert report index or durable outbox -> transition run to completed and publish `run_completed`. A successful terminal event therefore never precedes report visibility. On index failure, enqueue the exact metadata before completing; list/detail/download must read the index+outbox overlay. Preserve a bounded canonical-filesystem fallback for exact report-ID detail/download lookup if both index and outbox are unavailable. Retry on startup and a bounded 30-second background loop. Bind that loop to FastAPI lifespan with a stop event and deterministic shutdown, just like the watchdog. Startup rebuild must also index publishing runs recovered as completed by Task 3.

- [ ] **Step 6: Verify report-index tests GREEN**

  Run: `pytest -q tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_history.py tests/test_web_runner.py tests/test_web_api.py`

  Expected: all focused tests pass.

- [ ] **Step 7: Commit the indexed report read model**

  ```bash
  git add web/repositories.py web/history.py web/runner.py web/app.py tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_history.py tests/test_web_runner.py tests/test_web_api.py
  git commit -m "feat: add report index read model"
  ```

### Task 5: Add compatible pagination API and report-library controls

**Files:**
- Modify: `web/app.py`
- Modify: `web/static/index.html`
- Modify: `web/static/app.js`
- Modify: `web/static/styles.css`
- Test: `tests/test_web_api.py`
- Test: `tests/test_web_static.py`
- Test: `tests/test_web_browser.py`

- [ ] **Step 1: Write failing API pagination tests**

  Assert:

  - `/api/history` with no new query returns the original list;
  - any new parameter returns `{items,page,page_size,total,has_next}`;
  - page/page_size bounds and invalid date/sort return 422;
  - ticker exact match ignores query; query matches ticker/preview;
  - status and asset_type filters validate allowlists and filter server-side;
  - date bounds are inclusive on `analysis_date`;
  - both ascending and descending sorts place NULL generated times last and use report ID as tie-breaker;
  - outbox overlay is deduplicated before filters, sorting and total counting;
  - empty/out-of-range pages return 200 and empty items.

- [ ] **Step 2: Write failing UI tests**

  Require report library page state, previous/next controls, total count, empty page handling, request sequencing, and normalization of both the legacy list and envelope. Existing search/asset/status/sort controls must map directly to server parameters; changing a filter resets page to 1; the client must not re-filter a paged response as if it were the full dataset. Add `timed_out/分析超时` to the status filter.

- [ ] **Step 3: Run tests and verify RED**

  Run: `pytest -q tests/test_web_api.py tests/test_web_static.py tests/test_web_browser.py`

  Expected: pagination contract assertions fail; Playwright may skip only when its optional dependency is absent.

- [ ] **Step 4: Implement the compatible API**

  Detect whether any new query parameter was explicitly supplied. Preserve the exact legacy response otherwise. Validate bounds with FastAPI Query types and validate `date_from <= date_to` before repository search.

- [ ] **Step 5: Implement client pagination**

  Store `page/pageSize/total/hasNext/requestSeq`. Cancel or ignore stale history requests. Render Chinese paging controls and retain current filters when changing page.

- [ ] **Step 6: Verify pagination tests GREEN**

  Run: `pytest -q tests/test_web_api.py tests/test_web_static.py tests/test_web_browser.py`

  Expected: all installed focused tests pass.

- [ ] **Step 7: Commit report indexing and pagination**

  ```bash
  git add web tests
  git commit -m "feat: index and paginate analysis reports"
  ```

## Chunk 4: Provider Health and Quote Freshness

### Task 6: Persist and expose provider health

**Files:**
- Modify: `web/repositories.py`
- Modify: `web/market_models.py`
- Modify: `web/market_data.py`
- Modify: `web/app.py`
- Test: `tests/test_web_repositories.py`
- Test: `tests/test_web_market_data.py`
- Test: `tests/test_web_api.py`

- [ ] **Step 1: Write failing provider-health tests**

  With an injected clock, assert:

  - no credentials -> `not_configured`;
  - success -> `ready`, resets consecutive failures;
  - three consecutive failures or >50% failures in five minutes -> `degraded`;
  - explicit permanent provider error -> `error`;
  - at exactly 300 seconds all window counters including consecutive failures reset;
  - persisted state survives repository recreation;
  - error messages are redacted and length bounded.
  - request/failure counters, window start, success/failure times, latency, and error code persist and appear in the API projection;
  - fallback attempts are recorded against the provider actually invoked;
  - `NO_DATA` and `INVALID_SYMBOL` are symbol outcomes and do not degrade provider health;
  - `NOT_CONFIGURED` maps to not_configured; RATE_LIMITED/TIMEOUT/PROVIDER_ERROR count as transient failures; only an explicitly permanent adapter/configuration failure records error.

- [ ] **Step 2: Run tests and verify RED**

  Run: `pytest -q tests/test_web_repositories.py tests/test_web_market_data.py tests/test_web_api.py`

  Expected: health repository/service assertions fail.

- [ ] **Step 3: Implement ProviderHealthRepository**

  Provide `record_success`, `record_failure`, `mark_not_configured`, `get`, and `list`. Rotate the five-minute window before each update, classify only the current error, and sanitize diagnostics before persistence.

- [ ] **Step 4: Integrate health recording into ProviderRouter**

  Time every provider attempt. Record success/failure at the provider boundary, including fallbacks, without letting health persistence failure break quote delivery. Add the health projection to `/api/providers/market-data` and `/api/settings` while retaining current fields.

- [ ] **Step 5: Verify provider-health tests GREEN**

  Run: `pytest -q tests/test_web_repositories.py tests/test_web_market_data.py tests/test_web_api.py`

  Expected: all focused tests pass.

- [ ] **Step 6: Commit provider health persistence**

  ```bash
  git add web/repositories.py web/market_models.py web/market_data.py web/app.py tests/test_web_repositories.py tests/test_web_market_data.py tests/test_web_api.py
  git commit -m "feat: persist market provider health"
  ```

### Task 7: Normalize cache age and make 5-second refresh resilient

**Files:**
- Create: `web/static/quote-refresh.js`
- Create: `web/static/quote-refresh.test.js`
- Modify: `web/market_models.py`
- Modify: `web/market_data.py`
- Modify: `web/static/index.html`
- Modify: `web/static/app.js`
- Modify: `web/static/styles.css`
- Test: `tests/test_web_market_data.py`
- Test: `tests/test_web_static.py`
- Test: `web/static/quote-refresh.test.js`

- [ ] **Step 1: Write failing freshness matrix tests**

  Cover live success, provider-delayed success, provider failure with fresh cache, provider failure with stale cache, cache miss, total unavailable, fallback success, and missing quote time. Ensure cache hits never change original `quote_time` or `fetched_at`. Preserve old `fresh/stale` cache-status inputs while normalizing new responses to `live/hit/miss` plus compatible stale semantics. Verify `cache_status`, `provider_status`, and `stale_seconds` project through `QuoteSnapshot -> QuoteItem -> /api/quotes`.

- [ ] **Step 2: Write failing JavaScript timer tests**

  Extract a small refresh controller that is testable with fake timers. Assert 4-second AbortController timeout, sequence protection, 5/10/20/40/60-second backoff, success reset, visibility pause, and preservation of last successful quote values.

- [ ] **Step 3: Run tests and verify RED**

  Run:

  ```bash
  pytest -q tests/test_web_market_data.py tests/test_web_static.py
  node --test web/static/quote-refresh.test.js
  ```

  Expected: new freshness fields and refresh controller tests fail.

- [ ] **Step 4: Implement server freshness calculations**

  Compute stale seconds from `quote_time/as_of`, never from cache read time. Map provider/cache outcomes through one helper and include Chinese-safe provider diagnostics without changing raw price fields.

- [ ] **Step 5: Implement refresh controller and UI state labels**

  Create `web/static/quote-refresh.js`, load it before `app.js`, and use it for automatic/manual refresh. Render `实时/延迟/缓存/已过期/不可用`, provider name, quote time and last successful refresh time.

- [ ] **Step 6: Verify freshness and refresh tests GREEN**

  Run:

  ```bash
  pytest -q tests/test_web_market_data.py tests/test_web_static.py
  node --test web/static/quote-refresh.test.js
  ```

  Expected: all focused tests pass.

- [ ] **Step 7: Commit market observability**

  ```bash
  git add web tests
  git commit -m "feat: expose market data health"
  ```

## Chunk 5: Unified Chinese UI Resources

### Task 8: Centralize Chinese labels and dynamic value localization

**Files:**
- Create: `web/static/i18n.js`
- Create: `web/static/i18n.test.js`
- Modify: `web/static/index.html`
- Modify: `web/static/app.js`
- Modify: `web/market_localization.py`
- Modify: `web/app.py`
- Modify: `tests/test_web_static.py`
- Modify: `tests/test_web_api.py`

- [ ] **Step 1: Write failing i18n contract tests**

  Test one fixed `zh-CN` locale and stable keys for navigation, phases, agents, run statuses, provider statuses, freshness, asset types, exchanges, ratings and errors. Define deterministic fallbacks:

  - unknown non-empty key -> original value;
  - null -> `暂无` for user-facing values;
  - empty/whitespace -> `暂无`;
  - asset name/exchange -> Chinese value first, English/raw second, then `暂无`.

  Assert the page has no UI-language toggle and the analysis output language selector still works independently.

- [ ] **Step 2: Run tests and verify RED**

  Run:

  ```bash
  node --test web/static/i18n.test.js web/static/rating-labels.test.js
  pytest -q tests/test_web_static.py tests/test_web_api.py
  ```

  Expected: missing resource module and remaining inline-label assertions fail.

- [ ] **Step 3: Implement the resource module**

  Export browser/CommonJS helpers:

  ```javascript
  label(namespace, key, fallback)
  displayValue(value, placeholder = "暂无")
  assetIdentity(payload)
  ```

  Keep only `zh-CN` data this iteration. Unknown property/prototype keys must never return undefined.

- [ ] **Step 4: Route dynamic UI rendering through i18n**

  Replace duplicated inline status/phase/provider/freshness/error maps in `app.js`. Keep API raw values and downloads unchanged. Ensure model IDs and provider brands remain their official names where translation would be misleading, while surrounding descriptions are Chinese.

- [ ] **Step 5: Add stable API keys without removing labels**

  Add these exact compatibility fields:

  - `/api/config.analyst_options[*].label_key = analysts.<key>`;
  - `/api/providers/market-data.providers[*].status_key = provider_status.<status>`;
  - `/api/quotes.items[*].freshness_key = freshness.<freshness>` and `cache_status_key = cache_status.<cache_status>`;
  - serialized run records add `status_key = run_status.<status>`;
  - asset identity adds `exchange_key` only for a known exchange mapping, otherwise null.

  Preserve existing label, raw rating, raw exchange, raw provider, status, freshness and cache-status fields.

- [ ] **Step 6: Verify i18n tests GREEN**

  Run:

  ```bash
  node --test web/static/i18n.test.js web/static/rating-labels.test.js
  node --check web/static/app.js
  pytest -q tests/test_web_static.py tests/test_web_api.py
  ```

  Expected: all focused tests pass and no user-visible undefined values remain.

- [ ] **Step 7: Commit Chinese resource cleanup**

  ```bash
  git add web tests
  git commit -m "feat: unify Chinese web resources"
  ```

## Chunk 6: Full Verification and Documentation

### Task 9: Validate the integrated platform

**Files:**
- Modify: `README.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_web_browser.py`

- [ ] **Step 1: Update operational documentation**

  Document timeout settings, new `timed_out` state, report pagination compatibility, provider-health fields, SQLite v2 migration, and fixed Chinese UI versus independently selectable report language.

- [ ] **Step 2: Make CI enforce every installed check**

  Update `.github/workflows/ci.yml` to run `python -m compileall -q tradingagents cli web`, `ruff check .`, `node --test web/static/*.test.js`, and pytest. Add a separate Playwright job that installs the `web` extra, runs `python -m playwright install --with-deps chromium`, sets `TRADINGAGENTS_PLAYWRIGHT=1`, and executes `pytest -q tests/test_web_browser.py`. Extend that test with real navigation, paging, timeout-terminal and quote-refresh interactions using intercepted deterministic APIs.

- [ ] **Step 3: Run all static and unit checks**

  ```bash
  ruff check .
  python -m compileall -q tradingagents cli web
  node --test web/static/*.test.js
  node --check web/static/app.js
  pytest -q
  ```

  Expected: Ruff/compile/Node checks pass; pytest passes with only explicitly optional dependency skips.

- [ ] **Step 4: Run browser smoke tests**

  ```bash
  TRADINGAGENTS_PLAYWRIGHT=1 pytest -q tests/test_web_browser.py
  ```

  Expected: pass when Chromium is installed; otherwise install with `python -m playwright install chromium` and rerun.

- [ ] **Step 5: Run a clean-install and Web health smoke**

  Run this reproducible smoke script:

  ```bash
  smoke_dir="$(mktemp -d)"
  python -m venv "$smoke_dir/venv"
  "$smoke_dir/venv/bin/pip" install .
  "$smoke_dir/venv/bin/python" -m cli.main --help
  "$smoke_dir/venv/bin/python" -m cli.main web --host 127.0.0.1 --port 8765 >"$smoke_dir/web.log" 2>&1 &
  smoke_pid=$!
  trap 'kill "$smoke_pid" 2>/dev/null || true' EXIT
  ready=0
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if curl --silent --fail http://127.0.0.1:8765/api/config >/dev/null; then ready=1; break; fi
    sleep 1
  done
  test "$ready" -eq 1
  for path in / /api/config '/api/history?page=1&page_size=20' /api/providers/market-data /api/runs/active; do curl --fail "http://127.0.0.1:8765$path" >/dev/null; done
  kill "$smoke_pid"
  ```

- [ ] **Step 6: Review final diff and verify no unrelated changes**

  ```bash
  git diff --check
  git status --short
  ```

  Expected: only intended implementation, tests, migration and docs remain.

- [ ] **Step 7: Commit integrated documentation/CI changes**

  ```bash
  git add README.md .github/workflows/ci.yml tests/test_web_browser.py
  git commit -m "docs: document reliability operations"
  ```
