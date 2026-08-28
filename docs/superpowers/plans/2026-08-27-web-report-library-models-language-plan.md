# Web Report Library, Model Selection, and Output Languages Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a searchable report library, safe provider/model selectors, and explicit multilingual report output to the local Web console.

**Architecture:** Reuse `MODEL_OPTIONS` and `ReportHistory` as server-side sources of truth. Extend the validated run request and runner config with selected provider/models/language, then render the catalog and report metadata in the existing static client. Keep all credentials/endpoints process-local.

**Tech Stack:** FastAPI, Pydantic v2, static HTML/CSS/vanilla JavaScript, pytest, Playwright.

---

## Chunk 1: Backend Contracts and Configuration

**Files:** `web/models.py`, `web/app.py`, `web/runner.py`, `web/history.py`, `tests/test_web_models.py`, `tests/test_web_api.py`, `tests/test_web_runner.py`, `tests/test_web_history.py`

- [ ] Add fixed web provider/language constants and safe catalog projection from `MODEL_OPTIONS`.
- [ ] Extend `AnalysisRequest` with provider, quick/deep model, and output language fields; normalize omitted values against active config and reject invalid combinations with a stable generic 422 response.
- [ ] Extend `/api/config`, history list/detail, run records, and sidecar metadata with provider/model/language values while preserving nulls for legacy reports.
- [ ] Apply request choices to the copied runner config before graph construction.
- [ ] Write failing tests for catalog projection, request validation, sanitized errors, sidecar metadata, and legacy history fields; run them red.
- [ ] Implement the backend changes and run the focused Web tests green.

## Chunk 2: Report Library and Form Controls

**Files:** `web/static/index.html`, `web/static/styles.css`, `web/static/app.js`, `tests/test_web_static.py`

- [ ] Add report-library view, search/filter/sort controls, list/detail navigation, and complete-report fallback rendering.
- [ ] Add provider, quick model, deep model, and output-language controls with dependent model options and localized labels.
- [ ] Extend the bilingual dictionary for all new controls, statuses, metadata, and languages.
- [ ] Write/update static contract tests for the new DOM controls and state logic.
- [ ] Implement client rendering and run static tests plus JavaScript syntax checks.

## Chunk 3: Verification and Delivery

**Files:** `README.md`, `docs/superpowers/plans/2026-08-27-web-report-library-models-language-plan.md`

- [ ] Add concise Web configuration/report-library documentation.
- [ ] Run `ruff`, focused tests, full pytest, and browser smoke checks at desktop/mobile widths.
- [ ] Verify no secrets/endpoints appear in `/api/config` or run/history metadata.
- [ ] Commit the implementation in focused commits and leave `tradingagents web --port 8000` running.
