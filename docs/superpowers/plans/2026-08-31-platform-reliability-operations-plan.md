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
- Modify: `web/markdown.py`
- Modify: `web/market_localization.py`
- Modify: `web/models.py`
- Modify: `web/repositories.py`
- Modify: `web/storage.py`
- Modify: `cli/main.py`

- [ ] **Step 1: Record the failing lint baseline**

  Run: `ruff check .`

  Expected: FAIL with the current import-order, unused-variable, one-line-statement and typing-import violations.

- [ ] **Step 2: Apply mechanical Ruff-safe fixes**

  Use `ruff check . --fix` only for safe fixes, then manually expand one-line `if`/`try` statements in `web/repositories.py`, remove unused variables/imports, import `Iterator` from `collections.abc`, simplify the boolean return in `web/history.py`, and remove the duplicate `latest_content = content` assignment in `cli/main.py`. Do not alter public behavior.

- [ ] **Step 3: Replace the deprecated Starlette status constant**

  In `web/app.py`, replace `status.HTTP_422_UNPROCESSABLE_ENTITY` with the non-deprecated constant supported by the installed Starlette/FastAPI version. Add or update a focused API test that executes validation without emitting the deprecation warning.

- [ ] **Step 4: Verify the quality baseline**

  Run:

  ```bash
  ruff check .
  python -m compileall -q tradingagents cli web
  pytest -q tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_api.py
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
- Modify: `tradingagents/default_config.py`
- Test: `tests/test_web_storage.py`
- Test: `tests/test_web_models.py`
- Test: `tests/test_web_repositories.py`

- [ ] **Step 1: Write failing migration/model tests**

  Add tests proving:

  - an existing v1 database gains `last_heartbeat_at`, `timeout_at`, `terminal_reason`, and the three resolved timeout columns;
  - old rows remain readable with NULL lifecycle fields;
  - a migration failure leaves `schema_version` and prior tables unchanged;
  - `RunStatus.TIMED_OUT`, `EventName.RUN_TIMED_OUT`, `RunTimedOutPayload`, and the new `RunRecord` fields serialize correctly;
  - default timeout values are 7200/15/180 seconds and invalid configured values are clamped/rejected at the configuration boundary.

- [ ] **Step 2: Run focused tests and verify RED**

  Run: `pytest -q tests/test_web_storage.py tests/test_web_models.py tests/test_web_repositories.py`

  Expected: FAIL because migration v2 and lifecycle models do not exist.

- [ ] **Step 3: Implement migration v2 and idempotent compatibility helpers**

  Add v2 SQL for new tables and use `SQLiteStore` compatibility helpers to add columns only when missing. Migration execution must remain transactional and increment `schema_version` only after all statements succeed.

- [ ] **Step 4: Extend models and repository projections**

  Add:

  ```python
  class RunStatus(str, Enum):
      TIMED_OUT = "timed_out"

  class EventName(str, Enum):
      RUN_TIMED_OUT = "run_timed_out"
  ```

  Extend `RunRecord`, run snapshot payload, repository upsert/load, and persisted JSON with heartbeat/deadline/reason/config fields. Preserve `error_code` as the compatibility alias for terminal reasons.

- [ ] **Step 5: Verify migration/model tests GREEN**

  Run: `pytest -q tests/test_web_storage.py tests/test_web_models.py tests/test_web_repositories.py`

  Expected: all focused tests pass.

### Task 3: Implement watchdog, heartbeats, monotonic progress, and recovery

**Files:**
- Modify: `web/manager.py`
- Modify: `web/runner.py`
- Modify: `web/app.py`
- Modify: `web/static/app.js`
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
  - run row plus terminal event are written atomically or repaired on startup;
  - shutdown stops the watchdog thread.

- [ ] **Step 2: Write failing SSE/API/client tests**

  Assert `RunRecord` exposes heartbeat/deadline/reason, `run_timed_out` has the documented payload, the snapshot contains `snapshot_seq` and `replay_from_seq=snapshot_seq+1`, and the browser treats timed out as terminal even through the status polling fallback.

- [ ] **Step 3: Run lifecycle tests and verify RED**

  Run:

  ```bash
  pytest -q tests/test_web_manager.py tests/test_web_runner.py tests/test_web_api.py tests/test_web_static.py
  ```

  Expected: lifecycle and timeout assertions fail.

- [ ] **Step 4: Implement lifecycle CAS and watchdog**

  Add one locked transition helper that:

  ```python
  def _transition_terminal(run_id, allowed, status, event, payload):
      # compare current status, persist run+event in one transaction,
      # clear current_agent, release active_run_id, notify subscribers
  ```

  Start a daemon watchdog with a stoppable event. Check `timeout_at` and heartbeat lease every configured interval. Keep publishing excluded from heartbeat timeout, but repair it during startup recovery. All worker completion/publication calls must reject terminal records.

- [ ] **Step 5: Add worker heartbeat and remaining-deadline propagation**

  Touch heartbeat when a graph chunk, phase, activity or agent update is received. Calculate remaining seconds before provider/graph calls where a timeout option exists; never extend `timeout_at`. A timed-out worker returning later must observe the terminal CAS and skip report publication.

- [ ] **Step 6: Implement consistent SSE snapshots and browser terminal mapping**

  Build the run snapshot and sequence cut under the manager lock. Emit a synthetic snapshot without a conflicting SSE id, then replay only events after `snapshot_seq`. Add `timed_out` to all client terminal-state sets and translate it as `分析超时`.

- [ ] **Step 7: Run lifecycle tests GREEN**

  Run:

  ```bash
  pytest -q tests/test_web_manager.py tests/test_web_runner.py tests/test_web_api.py tests/test_web_static.py
  ```

  Expected: all lifecycle tests pass.

- [ ] **Step 8: Commit task reliability**

  ```bash
  git add web tradingagents/default_config.py tests
  git commit -m "feat: harden analysis task lifecycle"
  ```

## Chunk 3: SQLite Report Index and Server Pagination

### Task 4: Add report index, outbox, and rebuild path

**Files:**
- Modify: `web/migrations/002_reliability_operations.sql`
- Modify: `web/repositories.py`
- Modify: `web/history.py`
- Modify: `web/runner.py`
- Modify: `web/app.py`
- Test: `tests/test_web_storage.py`
- Test: `tests/test_web_repositories.py`
- Test: `tests/test_web_history.py`
- Test: `tests/test_web_runner.py`

- [ ] **Step 1: Write failing report-index tests**

  Cover:

  - reports/provider metadata and legacy nullable fields survive round-trip;
  - only allowlisted `root_name` and safe relative paths are accepted;
  - `path_state=missing/unsafe` records are excluded;
  - completed publish upserts an index row;
  - forced index-upsert failure writes `report_index_outbox` and the overlay keeps the new report visible;
  - retry removes outbox after successful upsert;
  - startup rebuild imports legacy/web reports with stable IDs and no duplicates;
  - deleting a file changes path state on rebuild without scanning each API request.

- [ ] **Step 2: Run focused report tests and verify RED**

  Run: `pytest -q tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_history.py tests/test_web_runner.py`

  Expected: FAIL because report index APIs do not exist.

- [ ] **Step 3: Implement ReportIndexRepository**

  Add explicit methods:

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

- [ ] **Step 4: Make ReportHistory an indexed read model**

  Keep Markdown/file detail reading and path safety in `ReportHistory`, but delegate list metadata to the index. Startup calls a bounded rebuild; detail lookup validates the indexed path and report gate. Preserve legacy list item fields exactly.

- [ ] **Step 5: Integrate publication and outbox retry**

  After the report directory is committed and the run is completed, upsert its metadata. On failure, enqueue the exact metadata. Retry on startup and a bounded 30-second background loop owned by app/manager lifecycle.

- [ ] **Step 6: Verify report-index tests GREEN**

  Run: `pytest -q tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_history.py tests/test_web_runner.py`

  Expected: all focused tests pass.

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
  - date bounds are inclusive on `analysis_date`;
  - stable ordering uses report ID; empty/out-of-range pages return 200 and empty items.

- [ ] **Step 2: Write failing UI tests**

  Require report library page state, previous/next controls, total count, empty page handling, request sequencing, and normalization of both the legacy list and envelope.

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
- Modify: `web/migrations/002_reliability_operations.sql`
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

### Task 7: Normalize cache age and make 5-second refresh resilient

**Files:**
- Modify: `web/market_models.py`
- Modify: `web/market_data.py`
- Modify: `web/static/app.js`
- Modify: `web/static/styles.css`
- Test: `tests/test_web_market_data.py`
- Test: `tests/test_web_static.py`
- Test: `web/static/quote-refresh.test.js`

- [ ] **Step 1: Write failing freshness matrix tests**

  Cover live success, provider-delayed success, provider failure with fresh cache, provider failure with stale cache, total unavailable, fallback success, missing quote time, and ensure cache hits never change quote time. Preserve existing `fresh/delayed/stale/unavailable` and old `cache_status=fresh/stale` handling while adding `provider_status` and `stale_seconds`.

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

  Add optional `label_key`, `status_key`, `exchange_key`, or equivalent stable fields where useful. Preserve current `label`, raw rating, raw exchange and raw provider fields for compatibility.

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
- Modify if needed: `README.md`
- Modify if needed: `.github/workflows/ci.yml`

- [ ] **Step 1: Run all static and unit checks**

  ```bash
  ruff check .
  python -m compileall -q tradingagents cli web
  node --test web/static/*.test.js
  node --check web/static/app.js
  pytest -q
  ```

  Expected: Ruff/compile/Node checks pass; pytest passes with only explicitly optional dependency skips.

- [ ] **Step 2: Run browser smoke tests**

  ```bash
  TRADINGAGENTS_PLAYWRIGHT=1 pytest -q tests/test_web_browser.py
  ```

  Expected: pass when Chromium is installed; otherwise install with `python -m playwright install chromium` and rerun.

- [ ] **Step 3: Run a clean-install and Web health smoke**

  Build/install in a temporary environment, verify `python -m cli.main --help`, start the Web console on an ephemeral port, and check `/`, `/api/config`, `/api/history?page=1&page_size=20`, `/api/providers/market-data`, and `/api/runs/active` return valid responses.

- [ ] **Step 4: Update operational documentation**

  Document timeout settings, new `timed_out` state, report pagination compatibility, provider-health fields, SQLite v2 migration, and fixed Chinese UI versus independently selectable report language.

- [ ] **Step 5: Review final diff and verify no unrelated changes**

  ```bash
  git diff --check
  git status --short
  ```

  Expected: only intended implementation, tests, migration and docs remain.

- [ ] **Step 6: Commit integrated documentation/CI changes**

  ```bash
  git add README.md .github/workflows/ci.yml
  git commit -m "docs: document reliability operations"
  ```
