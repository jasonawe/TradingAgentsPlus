# TradingAgents 个人投资者平台 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 TradingAgents Web 控制台上增加中文个人研究平台 MVP：默认关注列表、可追溯行情、资产分析入口、不可变报告和可扩展 SQLite 边界。

**Architecture:** 保留 FastAPI + 原生 HTML/CSS/JavaScript 和现有 TradingAgents Graph。新增薄领域层：SQLite migration/repository、QuoteProvider/ProviderRouter、SnapshotStore/DataSnapshotRecorder。MVP 只实现现有 yfinance/Alpha Vantage，报告和 snapshot 在同一发布目录中以原子协议提交；页面只调用领域 API，不直接读供应商或文件。

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, SQLite, yfinance, Alpha Vantage adapter, vanilla JavaScript, pytest, Playwright。

**Spec:** `docs/superpowers/specs/2026-08-27-personal-investor-platform-design.md`

---

## Chunk 1: Storage Boundary and SQLite Migrations

**Files:**
- Create: `web/storage.py`
- Create: `web/repositories.py`
- Create: `web/migrations/001_personal_platform.sql`
- Modify: `web/app.py`
- Modify: `web/manager.py`
- Test: `tests/test_web_storage.py`
- Test: `tests/test_web_repositories.py`
- Test: `tests/test_web_settings.py`
- Test: `tests/test_web_manager.py`

- [ ] **Step 1: Write failing migration and repository tests**

  Cover a temporary SQLite database with: schema version 1 creation, `web_runs` preservation, default watchlist creation, canonical-symbol uniqueness, market quote/candle tables, snapshot table, settings table, durable terminal events, migration rollback on invalid SQL, and startup lock behavior. Assert existing `web_runs` columns and persisted run metadata survive migration. Add settings precedence fixtures for `DEFAULT_CONFIG -> SQLite settings -> environment` and fallback to defaults when settings reads fail.

- [ ] **Step 2: Run storage tests to verify they fail**

  Run: `pytest -q tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_settings.py`

  Expected: collection or assertion failures because the storage module, migration, and repositories do not exist.

- [ ] **Step 3: Implement `web/storage.py`**

  Add a `SQLiteStore` that opens connections with row factories, `foreign_keys=ON`, busy timeout, and `BEGIN IMMEDIATE` for migrations. Create `schema_version` at version 0, execute `001_personal_platform.sql` inside a transaction, record version 1, and leave the database untouched if any migration fails. Use a process-level lock plus SQLite locking to prevent concurrent migration.

- [ ] **Step 4: Implement repository contracts**

  Add small repository classes with explicit methods:

  - `WatchlistRepository`: initialize/get default list, add/update/delete/reorder items with version CAS;
  - `QuoteRepository`: upsert/read latest quotes and candles, including freshness and source metadata;
  - `AnalysisRunRepository`: adapt the existing `web_runs` table without creating a parallel run table;
  - `SnapshotRepository`: save/read immutable manifest and dataset metadata;
  - `SettingsRepository`: read the non-secret settings whitelist and return per-field source metadata;
  - `ReportRepository`: read report directories only when the completion gate passes.

  Keep report bodies on disk. Store only references, hashes, statuses, and metadata in SQLite. Use parameterized SQL and map integrity errors to domain errors.

- [ ] **Step 5: Integrate store initialization into app startup**

  Update `create_app()` to construct one `SQLiteStore` under `results_dir` (or configured `web_runs_db` parent), run migrations before repositories, and expose repositories through `app.state`. Preserve injected fake manager/history dependencies used by current tests.

- [ ] **Step 6: Run storage and manager tests to verify they pass**

  Run: `pytest -q tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_settings.py tests/test_web_manager.py`

  Expected: all focused tests pass, including existing manager persistence tests.

- [ ] **Step 7: Commit the storage boundary**

  Run: `git add web/storage.py web/repositories.py web/migrations/001_personal_platform.sql web/app.py web/manager.py tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_settings.py tests/test_web_manager.py && git commit -m "feat: add personal platform sqlite boundary"`

## Chunk 2: Quote DTOs, Provider Routing, and Cache Semantics

**Files:**
- Create: `web/market_models.py`
- Create: `web/market_data.py`
- Create: `web/providers/yfinance_provider.py`
- Create: `web/providers/alpha_vantage_provider.py`
- Create: `web/providers/__init__.py`
- Modify: `tradingagents/default_config.py`
- Modify: `web/config.py`
- Test: `tests/test_web_market_data.py`
- Test: `tests/test_web_market_providers.py`

- [ ] **Step 1: Write failing DTO/router/provider tests**

  Test `QuoteSnapshot`, `Candle`, and identity validation; UTC serialization; `fresh|delayed|stale|unavailable`; `QuoteProvider` capability checks; explicit provider-chain fallback; `not_configured`, rate-limit, timeout, no-data, invalid-symbol, and provider-error behavior; 50-symbol limit; partial bulk results; stale cache hits; and null numeric fields for unavailable quotes. Mock all network calls. Assert that `invalid_symbol` and `no_data` stop the chain, while only `not_configured`, rate-limit, timeout, and provider-error advance to the next explicitly configured provider. Include mixed per-symbol outcomes and secret/header/full-URL redaction assertions. Use an injected clock and assert the default 60-second TTL plus env/SQLite override precedence.

- [ ] **Step 2: Run market tests to verify they fail**

  Run: `pytest -q tests/test_web_market_data.py tests/test_web_market_providers.py`

  Expected: missing module/type failures.

- [ ] **Step 3: Implement normalized market models**

  Define Pydantic/dataclass models for quote, candle, identity, item error, provider diagnostics, and bulk responses. Normalize all datetimes to UTC ISO-8601. Make `is_delayed` consistent with freshness: real-time `fresh` implies false; delayed/stale may be true; unavailable has null quote fields. Fix the protocol signatures to `QuoteProvider.supports(symbol, asset_type, capability) -> bool`, `get_quote(symbol, asset_type) -> QuoteSnapshot`, `get_candles(symbol, interval, start, end) -> list[Candle]`, and `get_identity(symbol, asset_type) -> AssetIdentity`; each receives no browser data and raises only the typed provider errors.

- [ ] **Step 4: Implement provider adapters**

  Wrap existing symbol normalization and data conventions. The yfinance adapter reads latest usable history/info without claiming exchange real-time entitlement. The Alpha Vantage adapter uses existing request/error helpers and recognizes missing credentials as `not_configured`. No provider may expose keys, headers, or full request URLs in errors.

- [ ] **Step 5: Implement `ProviderRouter` and `QuoteService`**

  Resolve only named strategy chains (`default-yfinance`, `fallback-yfinance-alpha-vantage`). Add `quote_ttl_seconds` to `DEFAULT_CONFIG` with default `60`, environment key `TRADINGAGENTS_QUOTE_TTL_SECONDS`, and the already planned SQLite settings override; inject a clock and repository into `QuoteService`. Route one symbol at a time, preserve the actual source, apply bounded timeout/retry behavior, read/write the quote repository cache, and return per-item errors without failing the whole bulk request. Compute stale state from the resolved TTL and preserve original quote time on cache hits. Never fall through on `invalid_symbol` or `no_data`; fall through only on the explicitly allowed typed errors.

- [ ] **Step 6: Add safe market settings projection**

  Extend `web/config.py` with fixed strategy metadata, provider capability/credential diagnostics, environment key `TRADINGAGENTS_QUOTE_STRATEGY`, and the resolved value/source projection used by both `/api/config` and `/api/settings`.

- [ ] **Step 7: Run market tests to verify they pass**

  Run: `pytest -q tests/test_web_market_data.py tests/test_web_market_providers.py tests/test_vendor_routing.py tests/test_no_data_handling.py`

  Expected: all mocked provider, fallback, freshness, and error tests pass.

- [ ] **Step 8: Commit the market layer**

  Run: `git add web/market_models.py web/market_data.py web/providers web/config.py tradingagents/default_config.py tests/test_web_market_data.py tests/test_web_market_providers.py && git commit -m "feat: add routed market quote service"`

## Chunk 3: Watchlist, Settings, and Analysis API Contracts

**Files:**
- Modify: `web/models.py`
- Modify: `web/app.py`
- Modify: `web/history.py`
- Modify: `web/manager.py`
- Modify: `web/runner.py`
- Test: `tests/test_web_api.py`
- Test: `tests/test_web_models.py`
- Test: `tests/test_web_history.py`

- [ ] **Step 1: Write failing API/model tests**

  Cover the exact MVP routes and schemas: `GET /api/watchlist`, add/update/delete/reorder semantics, duplicate canonical symbols, version conflicts, quote bulk response, candles and identity responses, `GET /api/providers/market-data`, `GET /api/settings`, and `POST /api/runs` accepting `quote_strategy_id`. Assert sanitized errors and no secret/endpoint leakage.

- [ ] **Step 2: Run API tests to verify they fail**

  Run: `pytest -q tests/test_web_api.py tests/test_web_models.py tests/test_web_history.py`

  Expected: route-not-found or schema assertion failures.

- [ ] **Step 3: Extend request and run contracts**

  Add `quote_strategy_id` to `AnalysisRequest`; add `publishing` and `interrupted` to `RunStatus`; add `run_interrupted` and `RunInterruptedPayload`; add `data_snapshot_id`, `data_status`, `reproducibility`, `effective_quote_strategy_id`, and effective provider chain to run/history/report metadata. Keep legacy fields nullable.

- [ ] **Step 4: Implement watchlist routes**

  Add the singular MVP routes only. Initialize `default` idempotently, normalize symbols before uniqueness checks, return 409 for duplicates/version conflicts, 422 for invalid bodies/order lists, 404 for missing items, and 204 with no body for successful DELETE. Return the documented watchlist/items/version envelope for other writes.

- [ ] **Step 5: Implement quote/candle/identity/settings routes**

  Add service-backed routes with bounded query validation. Return HTTP 200 for bulk quote partial/full provider failures; each unavailable item carries structured Chinese error and null prices. Add complete settings/provider projections with field-level source and strategy availability. Keep API keys/endpoints process-local.

- [ ] **Step 6: Apply field-level configuration precedence**

  Load `DEFAULT_CONFIG`, SQLite non-secret settings, then environment variables at startup. Resolve explicit request values last for that run. Quick analysis explicitly submits `Chinese`; custom analysis omission follows environment > SQLite > default. Return the effective values in config, run record, sidecar, and history.

- [ ] **Step 7: Test effective strategy and history metadata contracts**

  Add assertions for invalid `quote_strategy_id` -> 422 and effective strategy/provider-chain/source propagation in `/api/config`, `/api/settings`, run records, and history responses. Read `data_status`, `data_snapshot_id`, and reproducibility from sidecars when present; emit `unknown` for legacy reports. Defer physical report completion-gate enforcement and its fixtures to Chunk 4, after `SnapshotStore` and `COMMITTED` publication exist.

- [ ] **Step 8: Run API tests to verify they pass**

  Run: `pytest -q tests/test_web_api.py tests/test_web_models.py tests/test_web_history.py tests/test_web_command.py`

  Expected: all focused contract tests pass.

- [ ] **Step 9: Commit the API surface**

  Run: `git add web/models.py web/app.py web/history.py web/manager.py web/runner.py tests/test_web_api.py tests/test_web_models.py tests/test_web_history.py && git commit -m "feat: expose watchlist and market APIs"`

## Chunk 4: Snapshot-Aware Analysis and Atomic Report Publication

**Files:**
- Create: `web/snapshots.py`
- Modify: `tradingagents/graph/trading_graph.py`
- Modify: `tradingagents/agents/utils/agent_utils.py`
- Modify: `tradingagents/dataflows/interface.py`
- Modify: `web/runner.py`
- Modify: `web/history.py`
- Modify: `web/manager.py`
- Test: `tests/test_web_snapshots.py`
- Test: `tests/test_web_runner.py`
- Test: `tests/test_web_manager.py`
- Test: `tests/test_web_history.py`

- [ ] **Step 1: Write failing snapshot and lifecycle tests**

  Test deterministic canonical serialization for JSON, DataFrame, CSV, and text; manifest/payload hashes; path traversal and symlink rejection; cache read-before-network behavior; bypass failure; corrupt snapshot failure; `running -> publishing -> completed`; cancellation CAS; report gate; orphan quarantine; restart interruption; durable/synthesized interruption event; and idempotent terminal calls. Add explicit failure-injection tests for the required order: `running -> publishing` CAS is attempted before any publication and, if it fails, no publish is attempted and the run remains/returns `failed` with the reason; after a successful `running -> publishing`, a later `publishing -> completed` CAS failure quarantines the whole published report and persists the reason. Separately, fsync/rename/`COMMITTED` failures after a DB transition mark the run `failed`, quarantine temp/published output, and persist the reason. Cover successful cancellation removing `reports/.tmp/{run_id}` and parent-directory fsync/rename durability, including fsync after marker creation.

- [ ] **Step 2: Run snapshot tests to verify they fail**

  Run: `pytest -q tests/test_web_snapshots.py tests/test_web_runner.py tests/test_web_manager.py tests/test_web_history.py`

  Expected: missing snapshot/lifecycle behavior failures.

- [ ] **Step 3: Implement `SnapshotStore` and `DataSnapshotRecorder`**

  Place temporary run data under `reports/.tmp/{run_id}/snapshots/{run_id}/`; publish as `snapshots/{run_id}/manifest.json` plus `datasets/{dataset_key}.json` inside the same report directory. Implement canonicalization exactly as the spec states, fsync then rename, manifest schema validation, hash verification, and immutable finalization.

- [ ] **Step 4: Add snapshot-aware tool access**

  Add a concrete `SnapshotAwareDataProvider(snapshot_store, upstream_provider, recorder, run_id)` constructor and lookup contract (`get_dataset(dataset_key, request_fingerprint) -> DatasetPayload`), with an explicit miss/record path and hash verification. Thread this run-scoped provider through `TradingAgentsGraph` and the data tool boundary for every MVP dataset boundary: core stock quote/history, technical indicators, fundamentals, news, sentiment (Yahoo/StockTwits/Reddit), macro/FRED, and prediction-market data. Every tool must read an existing dataset payload before network access and record a miss. A tool that bypasses the provider fails the run with `reproducibility: unavailable`; no report is generated. Preserve existing direct programmatic test helpers only where they do not claim reproducibility.

- [ ] **Step 5: Implement atomic publication and recovery**

  Add manager CAS methods for `publishing`, terminal status, and durable terminal events. Enforce this exact sequence: (1) CAS `running -> publishing`; (2) write report, summary, sidecar (including `data_snapshot_id`, `data_status`, `reproducibility`, and effective provider chain), manifest, and payloads to one temporary directory; (3) fsync files and temporary/parent directories; (4) atomically rename the report directory; (5) write validated `COMMITTED`, fsync the marker and containing directory; (6) CAS `publishing -> completed`. Define asymmetric rollback explicitly: if step 1 CAS fails, do not publish and record a failed run reason; if step 6 DB commit fails after publication, quarantine the entire published directory and persist the failure reason; if steps 2-5 fsync/rename/`COMMITTED` operations fail after a DB transition, mark the run `failed`, quarantine any temp/published directory, and persist the reason. Coordinate the Chunk 1 `SnapshotRepository` metadata rows with this chunk's `SnapshotStore` filesystem manifest using the same `run_id` and manifest hash. History requires database `completed`, completed sidecar, `COMMITTED`, valid manifest, and valid dataset hashes. Startup moves orphaned directories to `quarantine/` and records the reason.

  Define two predicates: `artifact_gate` checks `COMMITTED`, completed sidecar, valid manifest/schema, and all dataset hashes without consulting the current DB status; `history_gate` is `artifact_gate` plus DB status `completed`. Define restart recovery by status: queued/running records become `interrupted`; publishing records become `completed` only when `artifact_gate` passes and the `publishing -> completed` CAS succeeds, otherwise clean up/quarantine output and mark `failed` with a reason. Test each branch deterministically.

- [ ] **Step 6: Add interruption and cancellation event behavior**

  Persist terminal events in `web_run_events`. On first event-stream connection for an old interrupted record without an event, synthesize one `run_interrupted` event and deduplicate it by run ID/sequence. Ensure cancellation cannot win after publishing begins, and ensure only the successful CAS path emits the terminal event.

- [ ] **Step 7: Run snapshot/lifecycle tests to verify they pass**

  Run: `pytest -q tests/test_web_snapshots.py tests/test_web_runner.py tests/test_web_manager.py tests/test_web_history.py`

  Expected: all snapshot integrity, restart, cancellation race, atomic publication, and history-gate tests pass.

- [ ] **Step 8: Commit snapshot-aware analysis**

  Run: `git add web/snapshots.py web/runner.py web/history.py web/manager.py tradingagents/graph/trading_graph.py tradingagents/agents/utils/agent_utils.py tradingagents/dataflows/interface.py tests/test_web_snapshots.py tests/test_web_runner.py tests/test_web_manager.py tests/test_web_history.py && git commit -m "feat: make web reports snapshot aware"`

## Chunk 5: Chinese Research Platform UI

**Files:**
- Modify: `web/static/index.html`
- Modify: `web/static/styles.css`
- Modify: `web/static/app.js`
- Create: `tests/fixtures/web_api.json`
- Test: `tests/test_web_static.py`
- Test: `tests/test_web_browser.py`

- [ ] **Step 1: Write failing static/browser tests**

  Assert that the product chrome is a single Simplified Chinese locale: remove the existing language toggle, English locale dictionary, English title/kickers/placeholders, and bilingual client behavior. Report output language remains a selectable form value sent to the run API, but is never a UI locale switch. Assert that no English navigation/button/status/error text remains and that all visible labels, ARIA names, placeholders, and tooltips are Chinese. The DOM must include default watchlist, quote state/source/time, asset detail, report library, quick/custom analysis entry, settings diagnostics, and separate run/report views. Add deterministic Playwright checks directly in `tests/test_web_browser.py` using Playwright's built-in request interception and fixed JSON fixtures declared in `tests/fixtures/web_api.json` (no live provider/LLM calls, no pytest-playwright plugin). The test module must start a temporary `ThreadingHTTPServer` on an ephemeral localhost port serving `web/static`, route `page.goto` to that URL, and intercept `/api/*` requests from the fixture, so it does not depend on an external process or port 8000. The exact command is `pytest -q tests/test_web_browser.py`; browser setup is `python -m playwright install chromium`. Cover success, partial/stale/unavailable quotes, watchlist CRUD/CAS/204 errors, identity/candles/config/settings responses, run completion, and report loading at desktop and narrow viewports; replace the current skip-only harness with this always-runnable local fixture.

- [ ] **Step 2: Run UI tests to verify they fail**

  Run: `pytest -q tests/test_web_static.py tests/test_web_browser.py`

  Expected: missing DOM/selector assertions and current bilingual UI failures.

- [ ] **Step 3: Replace the information architecture**

  Use Chinese-only navigation with four top-level views: “我的关注”, “资产详情”, “分析任务”, “报告库”; put settings in a diagnostics panel. Remove portfolio/holdings/P&L/trading language. Keep the report page independent from the run progress grid so completed reports never show “研究台进度”.

- [ ] **Step 4: Implement watchlist and asset interactions**

  Render loading, empty, fresh, delayed, stale, unavailable, partial, and retry states with source/as-of/freshness labels and a retry action. Wire add/update/delete/reorder to the documented watchlist API, including version/CAS conflict feedback and successful DELETE 204 handling. Add asset identity, quote source/time, chart range controls, recent reports, and a clear “开始分析” action. Use icon buttons only for familiar compact actions and provide Chinese accessible names/tooltips.

- [ ] **Step 5: Implement analysis and settings controls**

  Add quick/custom analysis entry. The quick action explicitly submits ticker, date, `output_language: "Chinese"`, default analyst team/depth, resolved provider, quick model, deep model, and quote strategy. The custom form preserves the eight CLI concepts in Chinese, including independent report language, LLM provider, quick/deep model, and quote strategy; when optional custom values are omitted, the request must preserve server precedence (environment > SQLite > default). Render dependent options from `/api/config` and show effective values from `/api/settings` without exposing secrets.

  Add report-library filters for ticker, analysis date range, rating, generated-time range, model/provider, and aggregate `data_status` using the API/spec values `complete`, `partial`, `unavailable`, `stale`, and legacy `unknown`. Treat `rating` as the canonical history field and map legacy sidecar `signal` to `rating` server-side (the compatibility `signal` field may remain read-only). Apply these filters client-side to the already loaded `/api/history` records: normalize analysis dates as `YYYY-MM-DD` and generated-time bounds as UTC ISO-8601 (inclusive lower, exclusive upper) before comparison; do not invent query parameters for the unparameterized history route. Keep dataset `freshness` (`fresh`, `delayed`, `stale`, `unavailable`) as a separate snapshot/detail field, never as a report-status filter. Add fixture and mapping tests that assert local history filtering uses canonical `rating` (including `signal -> rating` legacy mapping), `data_status`, and UTC generated-time bounds while snapshot filtering/display uses dataset `freshness`, including empty/loading/error states.

- [ ] **Step 6: Implement report reading and snapshot view**

  Keep conclusion-first summary, add data status/source/as-of metadata and a snapshot drawer/detail section. Render Markdown headings, lists, code, blockquotes, and tables safely; wrap wide tables on narrow screens. Use escaped content and fallback to complete report when section files are missing. Add executable safety tests with hostile raw HTML, `<script>`, `javascript:` and unsafe links, malformed tables, and cells containing HTML/quotes; assert no executable DOM or unsafe URL survives. Keep the report view structurally separate from the run view and assert the run progress grid is absent/hidden for completed and legacy reports.

  Add keyboard-only browser assertions for tab order, focus-visible styles, Enter/Space activation of controls, labelled form fields, live-region status updates, and ARIA names/roles. Check WCAG 2.1 AA contrast thresholds (normal text >=4.5:1, large text >=3:1) with a deterministic script in `tests/test_web_browser.py`, no horizontal overflow at the narrow viewport, and visible retry/error feedback.

- [ ] **Step 7: Run UI tests and browser smoke checks**

  Run: `pytest -q tests/test_web_static.py tests/test_web_browser.py`

  Then run the exact deterministic browser command `pytest -q tests/test_web_browser.py` (the test starts its own ephemeral static server and intercepts API calls with `tests/fixtures/web_api.json`, so it never contacts live providers or LLMs). Optionally start the real service with `python -m cli.main web --host 127.0.0.1 --port 8000` for a manual smoke check; it is not a prerequisite for the automated suite. Run the same suite at desktop and mobile viewport fixtures. Expected: Chinese UI, no overflow, correct report rendering, and no progress panel in completed report view.

- [ ] **Step 8: Commit the Chinese platform UI**

  Run: `git add web/static/index.html web/static/styles.css web/static/app.js tests/fixtures/web_api.json tests/test_web_static.py tests/test_web_browser.py && git commit -m "feat: redesign chinese investor research ui"`

## Chunk 6: Documentation, Full Verification, and Delivery

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-27-personal-investor-platform-design.md`
- Modify: `tests/test_web_api.py`
- Modify: `tests/test_web_history.py`
- Modify: `tests/test_web_static.py`

- [ ] **Step 1: Document data-source semantics**

  Update the existing README console section so it no longer claims an English locale, browser-locale detection, or a language toggle. Document a Simplified Chinese product chrome with report output language selected independently on each analysis request. Explain that watchlist quotes are read-only market snapshots, not broker execution; list yfinance/Alpha Vantage MVP behavior, timestamp/freshness labels, environment configuration, and how future Polygon/Twelve Data/Tushare/AKShare adapters will be added without changing the API. Document that report-library filters are client-side over the unparameterized `/api/history` response, with `data_status` and dataset `freshness` kept distinct.

- [ ] **Step 2: Run lint and targeted regression suites**

  Run: `ruff check web tradingagents tests/test_web_*.py`

  Run: `pytest -q tests/test_web_api.py tests/test_web_models.py tests/test_web_history.py tests/test_web_manager.py tests/test_web_runner.py tests/test_web_static.py tests/test_web_browser.py tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_settings.py tests/test_web_market_data.py tests/test_web_market_providers.py tests/test_web_snapshots.py`

  Expected: no lint errors and all targeted tests pass.

- [ ] **Step 3: Run the full regression suite**

  Run: `pytest -q`

  Expected: existing TradingAgents tests and all new platform tests pass; document any intentionally skipped external-provider tests.

- [ ] **Step 4: Verify security and recovery invariants**

  Run the focused security/recovery suites with `pytest -q tests/test_web_market_data.py tests/test_web_market_providers.py tests/test_web_api.py tests/test_web_browser.py tests/test_web_snapshots.py tests/test_web_manager.py tests/test_web_history.py`. Assert the fixtures contain no API keys, tokens, authorization headers, endpoint/full URLs, or unbounded raw provider payloads (each retained provider diagnostic/payload summary is capped at 64 KiB) in `/api/config`, `/api/settings`, `/api/providers/market-data`, run records, sidecars, or history responses. Exercise and assert service restart (`interrupted`/`publishing` recovery), stale cache, all-provider failure with null numeric fields, cancelled-run CAS, and orphan-report quarantine; verify one durable terminal event per terminal failure and manifest hash equality with its repository row.

- [ ] **Step 5: Verify the external browser URL**

  Start or restart the service with `python -m cli.main web --host 0.0.0.0 --port 8000`. If port 8000 is occupied, choose an unused numeric port (for example `WEB_PORT=8001`) and run `python -m cli.main web --host 0.0.0.0 --port "$WEB_PORT"`, recording the actual URL. Set `WEB_PORT=8000` for the default case. Discover a non-loopback address with `ipconfig getifaddr en0 || ipconfig getifaddr en1`; if neither returns an address, record that LAN verification is unavailable and still verify loopback. Check `curl -fsS -o /tmp/tradingagents-index.html -w '%{http_code}' "http://127.0.0.1:${WEB_PORT}/"` returns `200`, then run `rg -q '我的关注|资产详情|报告库' /tmp/tradingagents-index.html` and record both exit codes. When a LAN address is available, set `LAN_IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1)` and run the same checks against `"http://${LAN_IP}:${WEB_PORT}/"`; if the port is occupied or unreachable, report the exact conflict/connection result rather than treating it as a product failure. Leave a reachable service running for the user when startup succeeds.

- [ ] **Step 6: Commit documentation and final verification**

  Run: `git add README.md docs/superpowers/specs/2026-08-27-personal-investor-platform-design.md tests/test_web_api.py tests/test_web_history.py tests/test_web_static.py && git commit -m "docs: document personal investor platform"`

- [ ] **Step 7: Use verification-before-completion before reporting success**

  Before browser tests, run `python -m playwright install chromium` (or record that Chromium is already installed). Re-run the final focused commands explicitly: `ruff check web tradingagents tests/test_web_*.py` and `pytest -q tests/test_web_api.py tests/test_web_models.py tests/test_web_history.py tests/test_web_manager.py tests/test_web_runner.py tests/test_web_static.py tests/test_web_browser.py tests/test_web_storage.py tests/test_web_repositories.py tests/test_web_settings.py tests/test_web_market_data.py tests/test_web_market_providers.py tests/test_web_snapshots.py`; then run `pytest -q`. Inspect `git status --short`, and report exact pytest pass/skip counts, lint output, browser-install result, service URL/loopback and optional LAN result, and any remaining provider limitations. Do not claim completion without command evidence.
