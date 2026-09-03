# 2026-09-02 — Scheduled Analysis Jobs Design

**Date:** 2026-09-02
**Scope:** Per-asset automated analysis triggers (cron), raised manager concurrency,
persisted job state, dedicated UI page, and trigger history.

## Background

Today, every analysis run is started manually from the watchlist ("开始分析") or
the new-analysis form. Users who want a daily morning briefing must click into
each asset one by one. We need scheduled, per-asset triggers so the system can
run a configurable portfolio of analyses unattended.

Three constraints shape this design:

1. The active-run manager currently permits **exactly one** in-flight run
   (`web/manager.py:193` — "an analysis run is already active"). Multiple
   simultaneous triggers must therefore coexist; the manager's single-run
   invariant has to relax to a configurable upper bound.
2. Cron expressions need to survive service restarts without losing state, so
   job metadata must live in SQLite and be rehydrated on boot.
3. Each asset's "last successful run" parameters are the natural defaults for
   a scheduled trigger; we already persist `web_runs.request_json`, so the
   inference source exists.

## Goal

- Let each watchlist asset carry an optional cron expression that, when fired,
  starts an analysis with that asset's last successful parameter set.
- Run up to N analyses concurrently (N is a setting, default 3, range 1–10).
- Persist schedules in SQLite, reload on boot, never silently re-run missed
  triggers during downtime.
- Provide a dedicated "定时任务" page with full CRUD, a global on/off switch,
  manual "立即跑一次", and a per-trigger history view.

## Non-Goals

- Holiday calendars (no A-share holiday skipping).
- Multi-user / multi-tenant scheduling (single-user assumption holds).
- External cron delivery (we own the scheduler; no `crontab` integration).
- Email / push notifications on completion (out of scope; report library
  already surfaces finished runs).

## User Stories

1. As a user, I open `/scheduled`, see a table of all watchlist assets and
   their current cron expression (empty by default), and can add a cron to
   any row.
2. As a user, I can edit or delete a schedule, toggle it enabled/disabled, and
   trigger it immediately ("立即跑一次") to test my expression.
3. As a user, a master switch in the page header ("启用定时任务") lets me
   disable every trigger in one click.
4. As a user, after a trigger fires, I see its outcome (queued / running /
   succeeded / failed / skipped) in the per-job history log on the same page.
5. As a user, I restart the service; schedules persist; missed triggers do
   **not** run.

## Data Model

### New table: `scheduled_jobs`

```
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,            -- 600031.SS, BTC-USD, ...
    asset_type TEXT NOT NULL,        -- stock | crypto
    cron_expression TEXT NOT NULL,   -- standard 5-field cron, e.g. "30 9 * * 1-5"
    enabled INTEGER NOT NULL DEFAULT 1,  -- 0|1
    note TEXT,                        -- free-form reminder
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(symbol, asset_type)        -- one schedule per asset
);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_enabled
    ON scheduled_jobs(enabled);
```

### New table: `scheduled_run_logs`

```
CREATE TABLE IF NOT EXISTS scheduled_run_logs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES scheduled_jobs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,   -- ISO datetime the trigger targeted
    fired_at TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued | running | succeeded | failed | skipped
    run_id TEXT,                    -- the web_runs.run_id we created (nullable)
    skip_reason TEXT,               -- populated when status = skipped
    error TEXT,
    parameter_source TEXT           -- 'last_successful' | 'global_default'
);
CREATE INDEX IF NOT EXISTS idx_scheduled_run_logs_job
    ON scheduled_run_logs(job_id, fired_at DESC);
```

### Two new settings entries

- `scheduler.enabled` (bool, default `true`) — master switch.
- `scheduler.max_concurrent_runs` (int, default `3`, range 1–10) — raised
  manager cap.

These slot into the existing settings store alongside `quote_strategy_id`,
`watchlist_refresh_seconds`, etc.

## API Surface

All endpoints live under `/api/scheduled/...`. JSON in/out, same error shape
as existing endpoints (`{"detail": "..."}`).

### Reads

- `GET /api/scheduled/jobs` → `{items: ScheduledJob[], version}`
- `GET /api/scheduled/jobs/{id}` → `ScheduledJob`
- `GET /api/scheduled/settings` → `{enabled, max_concurrent_runs}`
- `GET /api/scheduled/jobs/{id}/logs?limit=20` → `{items: ScheduledRunLog[]}`

### Mutations

- `POST /api/scheduled/jobs` body: `{symbol, asset_type, cron_expression, note?}`
  → 201 + ScheduledJob
- `PATCH /api/scheduled/jobs/{id}` body: any subset of fields → 201
- `DELETE /api/scheduled/jobs/{id}` → 204
- `POST /api/scheduled/jobs/{id}/toggle` body: `{enabled: bool}` → 201
- `POST /api/scheduled/jobs/{id}/run` → 202, enqueues immediately
- `PATCH /api/scheduled/settings` body: any subset → 200

`ScheduledJob` shape:

```
{
  "id": "uuid",
  "symbol": "600031.SS",
  "asset_type": "stock",
  "cron_expression": "30 9 * * 1-5",
  "enabled": true,
  "note": "A 股开盘前",
  "created_at": "2026-09-02T...",
  "updated_at": "2026-09-02T...",
  "next_run_at": "2026-09-03T01:30:00+08:00",   // computed by APScheduler
  "last_run_at": "2026-09-02T01:30:00+08:00",   // from logs
  "last_run_status": "succeeded"                 // from logs
}
```

### Validation

- `cron_expression` must be a valid 5-field cron; reject anything else with
  422 and a human-readable message.
- `symbol` must exist in the watchlist (FK validated at API layer; if the user
  removes the asset from the watchlist, the schedule is deleted too via
  cascade).
- `asset_type` ∈ {stock, crypto}.

## Scheduler

- Library: **APScheduler** (`apscheduler>=3.10`, already a transitive dep of
  the project's other tooling; new direct dep).
- One `BackgroundScheduler` started in the FastAPI lifespan handler.
- Each row in `scheduled_jobs` (where `enabled = 1`) becomes a `cron` trigger
  added on boot and on every mutation.
- Job handler (sync from APScheduler's POV, runs the trigger logic on a
  worker thread):

  1. Re-check `scheduler.enabled` setting; if disabled, no-op.
  2. Re-check the job's `enabled`; if disabled, no-op.
  3. Look up the asset in the watchlist — if missing, write a `skipped` log
     and exit.
  4. Check "same asset already active" — if so, write `skipped` log with
     reason `asset_busy`, exit.
  5. Resolve parameters:
     - Read the most recent `web_runs` row for `ticker = symbol` with
     `status = 'completed'`; if present, take `request_json` as the
     `AnalysisRequest`.
     - Otherwise build a default `AnalysisRequest` (user's default provider /
     models / language from `/api/config`, depth 1, all analysts).
  6. Validate the request via the same pipeline as `POST /api/runs`. If
     validation fails, write `skipped` log with the reason.
  7. Call `active_manager.start_run(request, worker=worker)` — now permitted
     to succeed because the manager is concurrent.
  8. Insert a `scheduled_run_logs` row with `status = 'queued'` and the
     `run_id` returned.
  9. Listen for the run's terminal state via the existing manager event
     stream and update the log row to `succeeded` / `failed`.

- On boot: enumerate `scheduled_jobs`, register triggers. No catch-up — any
  trigger that should have fired while the service was down is **not** replayed.

## Manager Concurrency Changes

`web/manager.py` currently raises if a run is already active. We change it
to:

- Hold a `dict[run_id, RunRecord]` instead of a single record + flag.
- Track the count of in-flight runs; reject `start_run` when
  `len(in_flight) >= max_concurrent_runs` (max read from settings each call,
  default 3, range 1–10).
- The same-asset duplicate guard becomes: refuse to start a second run for a
  ticker that is already in `in_flight` (status `skipped` at scheduler level;
  the manual `/api/runs` endpoint returns 409 with a clear message).
- The WebSocket-style event stream multiplexes across all active runs, tagged
  with `run_id`.
- `GET /api/runs/active` becomes a list (was a single object).
- Existing callers that assume a single active run must be updated — this is
  the riskiest part of the change and warrants focused integration tests.

## UI

A new top-level nav item between "进行中" and "报告库":

```
<svg ...clock icon.../>
定时任务
```

### Page layout (`/scheduled`)

```
+-----------------------------------------------------------+
| 定时任务                          [☑ 启用定时任务]  [设置] |
+-----------------------------------------------------------+
| 表头: 资产 · Cron · 下次触发 · 上次结果 · 状态 · 操作    |
+-----------------------------------------------------------+
| 600031.SS  30 9 * * 1-5  明日 09:30  ✓ 2026-09-01  ⏷  |
|   三一重工 · "A 股开盘前"                  [▶][✎][✕]    |
+-----------------------------------------------------------+
| 688836.SS  0 15 * * 1-5  明日 15:00  —        ⏸ 禁用    |
|   昱舒科技                                  [▶][✎][✕]    |
+-----------------------------------------------------------+
| [+ 新增定时任务]                                          |
+-----------------------------------------------------------+
```

### Forms

- **Create / edit**: modal with fields `symbol` (autocomplete from
  watchlist), `cron_expression`, `note`. Live parser preview ("Next 3
  firings: …") so users can sanity-check the expression before saving.
- **Settings drawer** (right-side panel): `scheduler.max_concurrent_runs`
  slider 1–10 + master switch.

### Empty state

"还没有定时任务。点击「新增定时任务」开始，或在「我的关注」中点击资产的时钟图标。"

### History view

Clicking a job row expands a sub-table of recent `scheduled_run_logs` with
status chips and a deep link to the actual report (when status = succeeded).

## Internationalization

- New keys under `scheduler.*` namespace: `title`, `enabled`, `cronHint`,
  `cronPreview`, `maxConcurrent`, `addJob`, `editJob`, `deleteJob`,
  `runNow`, `runNowHint`, `nextRun`, `lastRun`, `never`, `skipped`,
  `skipped.asset_busy`, `skipped.no_params`, `invalidCron`, `jobSaved`.
- Help tooltip links to a small crontab reference (5-field English: minute
  hour day-of-month month day-of-week).

## Testing Strategy

### Unit tests

- `cron_parser` accepts standard 5-field expressions, rejects garbage, and
  returns the next N firing times.
- Parameter inference: given fixture `web_runs` rows, returns the most recent
  completed row's `request_json`.
- Fallback to global default when no completed row exists.
- `ScheduledJob` repository CRUD + cascade behaviour.

### Integration tests

- Scheduler end-to-end: insert a job with `cron_expression = "* * * * *"`
  (every minute), wait ≤ 65 s, assert `scheduled_run_logs` has a new row and
  `web_runs` has a corresponding `run_id`.
- Same-asset duplicate: trigger two cron firings within 1 s for the same
  ticker → second is `skipped (asset_busy)`.
- Disabled job: insert `enabled = 0`; assert no trigger fires.
- Master switch off: insert jobs, flip `scheduler.enabled = false`; assert
  no triggers fire.

### Manager concurrency tests

- Start 3 runs for 3 different symbols; all succeed in parallel.
- Start a 4th while 3 are active; 4th is rejected with a 409 (when max=3).
- Same symbol twice in parallel; second rejected (asset_busy).
- `/api/runs/active` returns the list of all in-flight.

### Frontend tests

- New view renders empty state.
- Create modal validates cron expression client-side and surfaces parser
  errors immediately.
- Toggle and run-now buttons fire the correct endpoints.
- History table updates when a new log row arrives via SSE/polling.

## Caller Impact (manager concurrency)

The single-run invariant leaks into a handful of call sites; they must be
updated together with `manager.py`.

**`web/app.py:424` `GET /api/runs/active`** — currently returns
`{"run": RunRecord | null}`. New shape:
`{"runs": [RunRecord, ...]}`. Empty list when nothing is in flight.

**`web/static/app.js:41` `showActive()`** — currently expects `response.run`;
will iterate `response.runs` and pick the most recent in-flight record to
attach the SSE stream to. A future enhancement (out of scope here) is a
list view of every active run on the page.

**`web/static/app.js:89-92` `restoreActiveRun()`** — same iteration; restores
the last-touched in-flight run on page reload.

**Event stream (`/api/runs/{id}/events`)** — already keyed by `run_id`
(`web/manager.py:629`), so no contract change. The SSE consumer must simply
be opened against the correct `run_id` returned from the list above.

**`POST /api/runs`** — the existing `ActiveRunError` ("an analysis run is
already active", line 477) is replaced by a new error class
`MaxConcurrentRunsError` when the cap is hit, and `AssetBusyError` when the
same ticker is already in flight. Both map to 409 with a clear message.

## Migration

`web/migrations/002_scheduled_jobs.sql` adds the two new tables. The current
schema is at version 1 in `web/storage.py`; bump to version 2 and add the
new file to the migration list.

## Risks & Mitigations

- **Manager concurrency is the biggest change.** Mitigate with extensive
  integration tests covering `/api/runs/active`, `/api/runs/{id}/events`,
  cancel/retry, and the SSE stream before shipping.
- **Cron timezone surprise.** Server local time is CST in our deployment,
  but the user might type UTC times by mistake. The cron preview shows the
  resolved timestamp with the timezone offset so the mistake is obvious
  before saving.
- **Missed triggers after restart.** Documented behaviour ("we don't replay
  missed triggers") rather than hidden; settings page shows "下次触发" so
  the user can confirm the next firing time is what they expect.
- **Schedule + watchlist deletion.** Removing a watchlist item should also
  delete its schedule. Implement as a single SQLite transaction in the
  watchlist DELETE handler.

## Open Questions

None — all design questions resolved during brainstorming.

## Implementation Status

- [x] Plan 1 — RunManager concurrency (`docs/superpowers/plans/2026-09-02-manager-concurrency.md`) — shipped
- [x] Plan 2 — Scheduled jobs core (data + APScheduler + trigger logic) — shipped
- [x] Plan 3 — Scheduled jobs UI — shipped

### Plan 1 summary

- `RunManager` now tracks a `_active_run_ids: set[str]` (legacy `active_run_id`
  property returns any one member for back-compat).
- `concurrent_runs_cap()` reads `scheduler.max_concurrent_runs` setting
  (default 3, clamped 1..10).
- `start_run` / `retry_run` go through `_check_admission_locked`, which raises
  `MaxConcurrentRunsError` or `AssetBusyError` instead of the old generic
  `ActiveRunError` (kept as an alias for back-compat).
- `ThreadPoolExecutor` resizes on every `start_run` so the cap is honoured.
- `GET /api/runs/active` returns `{"runs": [...]}` (was `{"run": ...}`).
- Front-end `showActive` / `restoreActiveRun` use a new `pickActiveRun()`
  helper that selects the most-recently-queued run.

### Plan 2 summary

- SQLite migration 005 persists `scheduled_jobs`, `scheduled_run_logs`, and
  scheduler settings; repository tests cover CRUD, validation, cascade, and
  last-successful parameter lookup.
- `ScheduledAnalysisService` owns APScheduler lifecycle, no-catch-up boot
  registration, exact nominal fire timestamps, admission skip logs, run
  submission, and terminal reconciliation.
- `/api/scheduled/...` exposes job CRUD, toggle, run-now, history, Cron preview,
  and scheduler settings. Watchlist deletion removes the asset schedule and logs
  transactionally and resyncs the scheduler.
- Retry preflight now uses the same per-asset and capacity admission checks as
  actual retry execution.

### Plan 3 summary

- `/scheduled` is a dedicated workspace with task table, master switch, create
  and edit form, live three-fire Cron preview, inline trigger history, run-now,
  enable/disable, delete confirmation, and a concurrency settings drawer.
- The page polls jobs and expanded logs every five seconds only while visible,
  preserves form input on recoverable errors, and supports keyboard focus return.
- Static tests and Playwright browser checks cover desktop and 390px mobile
  layouts without horizontal overflow.
