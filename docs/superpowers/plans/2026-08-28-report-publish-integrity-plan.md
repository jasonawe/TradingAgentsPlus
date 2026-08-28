# Decision Report Publish Integrity Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:test-driven-development when implementing this plan.

**Goal:** Ensure TradingAgents runs publish only complete, reproducible decision reports and never lists failed temporary reports as historical reports.

**Architecture:** Normalize final LangGraph state into deterministic JSON before writing the immutable data snapshot. Gate web report discovery on the same publish markers used by the repository (`run.json` with `status=completed` plus `COMMITTED`), so a failed or in-progress `.tmp` directory stays invisible.

**Tech Stack:** Python 3, pytest, LangChain message objects, SQLite-backed web run state, filesystem report store.

---

### Task 1: Serialize LangChain state safely

**Files:**
- Modify: `web/snapshots.py`
- Test: `tests/test_web_snapshots.py`

- [ ] Write a failing test proving `canonical_bytes` accepts a LangChain message inside the final state and emits deterministic JSON.
- [ ] Run the focused test and verify it fails with a JSON serialization error.
- [ ] Add recursive normalization for message objects and other structured values already used by final state, while preserving strict failure for unsupported objects.
- [ ] Run the snapshot test module and verify it passes.

### Task 2: Hide unpublished web reports from history

**Files:**
- Modify: `web/history.py`
- Test: `tests/test_web_history.py`

- [ ] Write a failing test proving a web report under `.tmp` without `run.json`/`COMMITTED` is excluded while a committed report remains visible.
- [ ] Run the focused test and verify it fails because the temporary report is indexed.
- [ ] Reuse the existing publish gate semantics when scanning web reports.
- [ ] Run the history and API tests and verify they pass.

### Task 3: Verify the publishing contract

**Files:**
- Review: `web/runner.py`, `web/repositories.py`, `web/synthesis.py`
- Test: `tests/test_web_runner.py`, `tests/test_web_api.py`

- [ ] Confirm snapshot creation remains before `COMMITTED` and that summary generation failure is represented as `summary_status=unavailable` without publishing a partial report.
- [ ] Run the complete web test slice, syntax checks, and `git diff --check`.
- [ ] Restart the web process if needed and report that the existing 600999 temporary run must be rerun to create a new complete report.
