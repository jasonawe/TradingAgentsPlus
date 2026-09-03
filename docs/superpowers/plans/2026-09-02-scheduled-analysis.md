# Scheduled Analysis Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement persisted per-asset cron analysis jobs with bounded concurrency, trigger history, settings, API, and a usable `/scheduled` console page.

**Architecture:** Keep scheduling inside the existing FastAPI process. SQLite stores jobs, logs, and scheduler settings; a small scheduler service owns APScheduler registration and invokes the existing `RunManager`/`WebRunRunner` path. The browser consumes JSON endpoints from the existing single-page console and uses the same local i18n and safe-rendering conventions.

**Tech Stack:** Python 3.10+, FastAPI, SQLiteStore migrations, APScheduler 3.x, Pydantic, vanilla JavaScript/CSS, pytest.

---

## Chunk 1: Persistence and deterministic scheduling primitives

**Files:**
- Create: `web/migrations/005_scheduled_analysis.sql`
- Create: `web/scheduled.py`
- Modify: `web/repositories.py`
- Modify: `web/config.py`
- Modify: `web/manager.py`
- Modify: `web/app.py`
- Test: `tests/test_scheduled.py`, `tests/test_web_repositories.py`, `tests/test_web_storage.py`

- [x] Add migration tables and indexes for `scheduled_jobs` and `scheduled_run_logs`.
- [x] Add repository CRUD, log transitions, latest successful request lookup, and watchlist existence checks.
- [x] Add strict five-field Cron validation and next-fire calculation with timezone-aware datetimes.
- [x] Add scheduler settings to the settings whitelist and configure the manager from persisted settings at app construction.
- [x] Add tests for validation, CRUD/cascade, settings persistence, and parameter fallback.

## Chunk 2: Scheduler service and API

**Files:**
- Create: `web/scheduler.py`
- Modify: `web/app.py`
- Modify: `web/repositories.py`
- Modify: `web/runner.py`
- Modify: `web/migrations/005_scheduled_analysis.sql`
- Modify: `pyproject.toml`
- Test: `tests/test_scheduled.py`, `tests/test_web_api.py`

- [x] Add APScheduler dependency and a service that reloads enabled jobs on boot without catch-up.
- [x] Implement scheduled trigger admission, skip logging, parameter inference, run submission, and terminal log reconciliation.
- [x] Add `/api/scheduled/jobs`, job detail/logs, CRUD, toggle, run-now, and settings endpoints.
- [x] Start/stop the service in FastAPI lifespan and resync it after mutations.
- [x] Add integration tests with a deterministic scheduler or direct trigger invocation.

## Chunk 3: Watchlist and retry integration

**Files:**
- Modify: `web/repositories.py`
- Modify: `web/app.py`
- Modify: `web/manager.py`
- Test: `tests/test_scheduled.py`, `tests/test_run_manager_concurrency.py`

- [x] Delete schedules transactionally when their watchlist asset is deleted.
- [x] Make retry admission honor the configured capacity and same-asset guard rather than the legacy any-active guard.
- [x] Verify concurrent manual and scheduled runs return/record the correct 409 or skipped outcome.

## Chunk 4: Scheduled analysis UI

**Files:**
- Modify: `web/static/index.html`
- Modify: `web/static/app.js`
- Modify: `web/static/i18n.js`
- Modify: `web/static/styles.css`
- Test: `tests/test_web_static.py`, `web/static/*.test.js`

- [x] Add nav item and `/scheduled` view with empty state, job table, settings controls, and history expansion.
- [x] Add create/edit modal with watchlist autocomplete and live Cron preview/validation.
- [x] Wire toggle, delete, run-now, settings save, and report deep links.
- [x] Add scheduler i18n keys and responsive styling.

## Chunk 5: Verification and documentation

- [x] Run focused scheduled/API/static tests and the full suite.
- [x] Update the design status block and README with the delivered scheduler behavior.
- [ ] Refresh the knowledge graph incrementally after implementation and validate its metadata/fingerprints.
