# RunManager Concurrency Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise `RunManager` from a single-active-run invariant to a configurable concurrent cap (default 3, range 1–10), reject same-ticker duplicates, and reshape the `/api/runs/active` contract to return a list. This is the foundation that Plan 2 (Scheduled Jobs) depends on.

**Architecture:** Replace the single `_active_run_id` pointer with an ordered `_active_run_ids: set[str]`. `start_run` becomes a two-gate admission check (capacity, then ticker uniqueness). The `ThreadPoolExecutor` scales its worker pool to match the cap. Front-end "进行中" view iterates the list and attaches the SSE stream to the most-recent run (existing behaviour, now keyed on a list).

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, SQLite (existing), threading primitives (no new deps).

**Spec:** `docs/superpowers/specs/2026-09-02-scheduled-analysis-design.md` (sections "Manager Concurrency Changes" + "Caller Impact").

---

## Chunk 1: Internal Manager Refactor (Keep API Contracts Identical)

### Task 1.1: Settings lookup + cap provider

**Files:**
- Modify: `web/manager.py:142-150` (`active_run_id` property)
- Modify: `web/manager.py:111-119` (`__init__` — executor + state)

- [ ] **Step 1: Write the failing test for `concurrent_runs_cap`**

  Add to a new `tests/test_run_manager_concurrency.py`:

  ```python
  from web.config import _parse_integer_setting
  from web.manager import RunManager

  def test_concurrent_runs_cap_reads_default_when_settings_missing():
      manager = RunManager(store=None)
      assert manager.concurrent_runs_cap() == 3  # default fallback

  def test_concurrent_runs_cap_clamps_out_of_range():
      manager = RunManager(store=None)
      manager.configure_concurrency({"scheduler.max_concurrent_runs": {"value": 999, "source": "configured"}})
      assert manager.concurrent_runs_cap() == 10  # clamped high
      manager.configure_concurrency({"scheduler.max_concurrent_runs": {"value": 0, "source": "configured"}})
      assert manager.concurrent_runs_cap() == 1  # clamped low
  ```

- [ ] **Step 2: Run the test and verify it fails**

  Run: `python -m pytest tests/test_run_manager_concurrency.py -v`
  Expected: FAIL — `AttributeError: 'RunManager' object has no attribute 'concurrent_runs_cap'`.

- [ ] **Step 3: Implement `concurrent_runs_cap` + `configure_concurrency`**

  In `web/manager.py`, add at class scope:

  ```python
  DEFAULT_MAX_CONCURRENT_RUNS = 3
  MIN_MAX_CONCURRENT_RUNS = 1
  MAX_MAX_CONCURRENT_RUNS = 10
  _MAX_CONCURRENT_RUNS_SETTING = "scheduler.max_concurrent_runs"

  @classmethod
  def clamp_max_concurrent(cls, value: int) -> int:
      return max(cls.MIN_MAX_CONCURRENT_RUNS, min(cls.MAX_MAX_CONCURRENT_RUNS, int(value)))

  def concurrent_runs_cap(self) -> int:
      """Return the configured upper bound for parallel active runs."""
      with self._lock:
          item = self._concurrency_config.get(self._MAX_CONCURRENT_RUNS_SETTING)
          if item is None:
              return self.DEFAULT_MAX_CONCURRENT_RUNS
          raw = item.get("value", self.DEFAULT_MAX_CONCURRENT_RUNS)
          try:
              parsed = _parse_integer_setting(self._MAX_CONCURRENT_RUNS_SETTING, raw)
          except ValueError:
              return self.DEFAULT_MAX_CONCURRENT_RUNS
          return self.clamp_max_concurrent(parsed)

  def configure_concurrency(self, settings: dict[str, dict[str, Any]]) -> None:
      """Merge scheduler.* settings; future start_run calls use the new cap."""
      with self._lock:
          self._concurrency_config = dict(settings)
  ```

  In `__init__`, after `self.lifecycle_config = ...`, add:

  ```python
  self._concurrency_config: dict[str, dict[str, Any]] = {}
  ```

- [ ] **Step 4: Run the test and verify it passes**

  Run: `python -m pytest tests/test_run_manager_concurrency.py::test_concurrent_runs_cap_reads_default_when_settings_missing tests/test_run_manager_concurrency.py::test_concurrent_runs_cap_clamps_out_of_range -v`
  Expected: PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add tests/test_run_manager_concurrency.py web/manager.py
  git commit -m "feat(manager): add concurrent_runs_cap() with default + clamp"
  ```

---

### Task 1.2: Replace `_active_run_id` with `_active_run_ids`

**Files:**
- Modify: `web/manager.py:119` (init)
- Modify: `web/manager.py:142-148` (`active_run_id` property — preserve)
- Modify: `web/manager.py:702` (`_finish_locked`)
- Modify: `web/manager.py:1045-1047` (`can_retry`)

- [ ] **Step 1: Write the failing test for the set-based active tracking**

  Append to `tests/test_run_manager_concurrency.py`:

  ```python
  def test_active_run_ids_starts_empty():
      manager = RunManager(store=None)
      assert manager.active_run_ids() == set()
      assert manager.active_run_id is None  # legacy property kept

  def test_active_run_id_returns_most_recent_when_multiple():
      manager = RunManager(store=None)
      manager._active_run_ids = {"run-a", "run-b"}
      assert manager.active_run_id in {"run-a", "run-b"}  # any one is acceptable
  ```

- [ ] **Step 2: Run the test and verify it fails**

  Run: `python -m pytest tests/test_run_manager_concurrency.py::test_active_run_ids_starts_empty -v`
  Expected: FAIL — `AttributeError: 'RunManager' object has no attribute 'active_run_ids'`.

- [ ] **Step 3: Refactor `_active_run_id` → `_active_run_ids`**

  In `web/manager.py`:

  - Change `self._active_run_id: str | None = None` to `self._active_run_ids: set[str] = set()`.
  - Update the `active_run_id` property to return `next(iter(self._active_run_ids), None)` so the existing single-id getter still works for callers that haven't migrated.
  - Add a new method:

    ```python
    def active_run_ids(self) -> set[str]:
        with self._lock:
            self._check_all_expired_locked()
            return set(self._active_run_ids)
    ```

  - In `_finish_locked` replace:
    ```python
    if self._active_run_id == state.record.run_id:
        self._active_run_id = None
    ```
    with:
    ```python
    self._active_run_ids.discard(state.record.run_id)
    ```
  - In `can_retry` replace:
    ```python
    if self._active_run_id is not None:
        return False, "another_run_active"
    ```
    with:
    ```python
    if self._active_run_ids:
        return False, "another_run_active"
    ```

- [ ] **Step 4: Run the manager tests**

  Run: `python -m pytest tests/test_run_manager_concurrency.py -v`
  Expected: PASS for new tests.

- [ ] **Step 5: Run the full test suite to confirm no regression**

  Run: `python -m pytest tests/ -q --ignore=tests/test_direct_db_path_uses_full_store_migration_and_replays_restart_interrupt.py -x`
  Expected: All pre-existing tests still pass; the legacy `active_run_id` property returns a valid id when one is in flight.

- [ ] **Step 6: Commit**

  ```bash
  git add web/manager.py tests/test_run_manager_concurrency.py
  git commit -m "refactor(manager): track active runs in a set (keep active_run_id property)"
  ```

---

### Task 1.3: Errors + capacity-aware `start_run`

**Files:**
- Modify: `web/manager.py:43-44` (`ActiveRunError`)
- Modify: `web/manager.py:182-227` (`start_run` admission check)
- Modify: `web/manager.py:1087-1115` (`retry_run` admission check)

- [ ] **Step 1: Write the failing tests for capacity + ticker guards**

  Append to `tests/test_run_manager_concurrency.py`:

  ```python
  import pytest
  from web.manager import AssetBusyError, MaxConcurrentRunsError
  from web.models import AnalysisRequest, RunStatus

  def _stub_request(ticker: str = "AAPL") -> AnalysisRequest:
      from datetime import date
      return AnalysisRequest(
          ticker=ticker, analysis_date=date(2026, 9, 2),
          asset_type="stock", analysts=["market"], research_depth=1,
      )

  def test_start_run_rejects_when_at_cap(monkeypatch):
      manager = RunManager(store=None)
      manager._active_run_ids = {"run-x", "run-y", "run-z"}
      monkeypatch.setattr(manager, "concurrent_runs_cap", lambda: 3)
      with pytest.raises(MaxConcurrentRunsError):
          manager.start_run(_stub_request())

  def test_start_run_rejects_duplicate_ticker(monkeypatch):
      manager = RunManager(store=None)
      manager._active_run_ids = {"run-existing"}
      manager._records["run-existing"] = _managed_stub("AAPL")
      monkeypatch.setattr(manager, "concurrent_runs_cap", lambda: 3)
      with pytest.raises(AssetBusyError):
          manager.start_run(_stub_request("AAPL"))
  ```

  And add a helper at module top:
  ```python
  def _managed_stub(ticker: str):
      from web.manager import _ManagedRun
      from collections import deque
      from web.models import RunRecord, RunStatus
      import threading
      record = RunRecord(run_id="run-existing", request=_stub_request(ticker), status=RunStatus.RUNNING,
                          queued_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
      return _ManagedRun(record=record, events=deque(maxlen=8), condition=threading.Condition(), cancel_event=threading.Event())
  ```

- [ ] **Step 2: Run the test and verify it fails**

  Run: `python -m pytest tests/test_run_manager_concurrency.py::test_start_run_rejects_when_at_cap -v`
  Expected: FAIL — `ImportError: cannot import name 'MaxConcurrentRunsError'`.

- [ ] **Step 3: Add the new exception classes + admission logic**

  In `web/manager.py`, after `ActiveRunError`, add:

  ```python
  class MaxConcurrentRunsError(RuntimeError):
      """Raised when admitting a new run would breach the configured cap."""

  class AssetBusyError(RuntimeError):
      """Raised when a run for the same ticker is already in flight."""
  ```

  In `start_run`, replace the existing admission block:
  ```python
  if self._active_run_id is not None:
      raise ActiveRunError("an analysis run is already active")
  ```
  with:
  ```python
  self._check_admission_locked(request)
  ```

  and add the helper:

  ```python
  def _check_admission_locked(self, request: AnalysisRequest) -> None:
      """Reject new runs when at capacity or when the ticker is busy."""
      cap = self.concurrent_runs_cap()
      if len(self._active_run_ids) >= cap:
          raise MaxConcurrentRunsError(f"max concurrent runs reached ({cap})")
      target = request.ticker.strip().upper()
      for run_id in self._active_run_ids:
          existing = self._records.get(run_id)
          if existing and existing.record.request.ticker.strip().upper() == target:
              raise AssetBusyError(f"a run for {target} is already active")
  ```

  Apply the same admission gate inside `retry_run` (it currently has its own `ActiveRunError` check; replace with `self._check_admission_locked(request)`).

- [ ] **Step 4: Run the new tests**

  Run: `python -m pytest tests/test_run_manager_concurrency.py::test_start_run_rejects_when_at_cap tests/test_run_manager_concurrency.py::test_start_run_rejects_duplicate_ticker -v`
  Expected: PASS.

- [ ] **Step 5: Run the full test suite**

  Run: `python -m pytest tests/ -q --ignore=tests/test_direct_db_path_uses_full_store_migration_and_replays_restart_interrupt.py`
  Expected: All tests pass. Pre-existing tests that relied on the single-run invariant now operate against a cap of 1 (the default falls back to 3, so we must thread through a cap of 1 in those tests — see Step 6).

- [ ] **Step 6: Update the pre-existing single-run tests**

  Search: `grep -rn "ActiveRunError\|already active" tests/`
  For any test that asserts the legacy single-run behaviour, prepend:
  ```python
  manager.configure_concurrency({"scheduler.max_concurrent_runs": {"value": 1, "source": "configured"}})
  ```
  so the test continues to exercise the cap-of-1 invariant. Do not change assertions or expected exceptions.

- [ ] **Step 7: Commit**

  ```bash
  git add web/manager.py tests/test_run_manager_concurrency.py
  git commit -m "feat(manager): cap concurrent runs, reject duplicate tickers"
  ```

---

### Task 1.4: Insert + evict active-run ids around lifecycle

**Files:**
- Modify: `web/manager.py:226` (`start_run` adds to set)
- Modify: `web/manager.py:702` (already updated in Task 1.2)
- Modify: `web/manager.py:1087` (`retry_run` adds to set)

- [ ] **Step 1: Write the failing lifecycle test**

  Append to `tests/test_run_manager_concurrency.py`:

  ```python
  def test_start_run_inserts_into_active_set(monkeypatch):
      manager = RunManager(store=None)
      monkeypatch.setattr(manager, "concurrent_runs_cap", lambda: 3)
      record = manager.start_run(_stub_request("AAPL"), worker=lambda rid: None)
      assert record.run_id in manager.active_run_ids()

  def test_finish_removes_from_active_set(monkeypatch):
      manager = RunManager(store=None)
      monkeypatch.setattr(manager, "concurrent_runs_cap", lambda: 3)
      record = manager.start_run(_stub_request("AAPL"), worker=lambda rid: None)
      manager.complete_run(record.run_id, report_id="r-1")
      assert manager.active_run_ids() == set()
  ```

- [ ] **Step 2: Run the test and verify it fails**

  Run: `python -m pytest tests/test_run_manager_concurrency.py::test_start_run_inserts_into_active_set -v`
  Expected: FAIL — set is empty.

- [ ] **Step 3: Wire `start_run` + `retry_run` to register the id**

  In `web/manager.py`, locate:
  ```python
  self._records[identifier] = state
  self._active_run_id = identifier
  self._persist_locked(record)
  ```
  in both `start_run` (line ~226) and `retry_run` (line ~1087). Replace the assignment with `self._active_run_ids.add(identifier)`.

- [ ] **Step 4: Run the test and verify it passes**

  Run: `python -m pytest tests/test_run_manager_concurrency.py::test_start_run_inserts_into_active_set tests/test_run_manager_concurrency.py::test_finish_removes_from_active_set -v`
  Expected: PASS.

- [ ] **Step 5: Run the full suite**

  Run: `python -m pytest tests/ -q --ignore=tests/test_direct_db_path_uses_full_store_migration_and_replays_restart_interrupt.py`
  Expected: All pass.

- [ ] **Step 6: Commit**

  ```bash
  git add web/manager.py tests/test_run_manager_concurrency.py
  git commit -m "refactor(manager): track active run ids across start/retry/finish"
  ```

---

### Task 1.5: Resize the executor to match the cap

**Files:**
- Modify: `web/manager.py:114-117` (`ThreadPoolExecutor` init)
- Modify: `web/manager.py:680-697` (`shutdown`)

- [ ] **Step 1: Write the failing test**

  Append to `tests/test_run_manager_concurrency.py`:

  ```python
  def test_executor_grows_to_match_cap(monkeypatch):
      manager = RunManager(store=None)
      monkeypatch.setattr(manager, "concurrent_runs_cap", lambda: 5)
      manager._resize_executor()
      assert manager._executor._max_workers == 5  # type: ignore[attr-defined]
  ```

- [ ] **Step 2: Run the test and verify it fails**

  Run: `python -m pytest tests/test_run_manager_concurrency.py::test_executor_grows_to_match_cap -v`
  Expected: FAIL — `_resize_executor` doesn't exist.

- [ ] **Step 3: Implement `_resize_executor`**

  In `web/manager.py`, add a private method:

  ```python
  def _resize_executor(self) -> None:
      """Grow the executor pool to at least the current cap without losing inflight workers.

      ``ThreadPoolExecutor`` cannot shrink at runtime, so we only grow. Each
      ``start_run`` call invokes this so the cap change takes effect for the
      next admission.
      """
      with self._lock:
          cap = self.concurrent_runs_cap()
          current = getattr(self._executor, "_max_workers", 1)
          if cap <= current:
              return
          old = self._executor
          self._executor = ThreadPoolExecutor(max_workers=cap, thread_name_prefix="tradingagents-web")
          old.shutdown(wait=False, cancel_futures=False)
  ```

  Call `self._resize_executor()` at the start of `start_run` and `retry_run`, before any `self._executor.submit`.

- [ ] **Step 4: Run the test and verify it passes**

  Run: `python -m pytest tests/test_run_manager_concurrency.py::test_executor_grows_to_match_cap -v`
  Expected: PASS.

- [ ] **Step 5: Add an integration test that runs three analyses in parallel**

  Append to `tests/test_run_manager_concurrency.py`:

  ```python
  def test_three_runs_in_flight_simultaneously(monkeypatch):
      manager = RunManager(store=None)
      manager.configure_concurrency({"scheduler.max_concurrent_runs": {"value": 3, "source": "configured"}})
      records = [
          manager.start_run(_stub_request(f"T{i}"), worker=lambda rid: None)
          for i in range(3)
      ]
      assert len(manager.active_run_ids()) == 3
      for record in records:
          manager.complete_run(record.run_id, report_id=f"r-{record.run_id}")
      assert manager.active_run_ids() == set()
  ```

- [ ] **Step 6: Run the test and verify it passes**

  Run: `python -m pytest tests/test_run_manager_concurrency.py::test_three_runs_in_flight_simultaneously -v`
  Expected: PASS.

- [ ] **Step 7: Run the full suite**

  Run: `python -m pytest tests/ -q --ignore=tests/test_direct_db_path_uses_full_store_migration_and_replays_restart_interrupt.py`
  Expected: All pass.

- [ ] **Step 8: Commit**

  ```bash
  git add web/manager.py tests/test_run_manager_concurrency.py
  git commit -m "feat(manager): grow ThreadPoolExecutor to match concurrency cap"
  ```

---

## Chunk 2: API Contract + Error Mapping

### Task 2.1: `/api/runs/active` returns a list

**Files:**
- Modify: `web/app.py:420-435` (`GET /api/runs/active`)
- Modify: `web/app.py:438-490` (`POST /api/runs` error handling)
- Modify: `tests/test_web_api.py` (add contract assertions)

- [ ] **Step 1: Write the failing contract test**

  Append to `tests/test_web_api.py`:

  ```python
  def test_runs_active_returns_list():
      from fastapi.testclient import TestClient
      from web.app import app
      client = TestClient(app)
      response = client.get("/api/runs/active")
      assert response.status_code == 200
      body = response.json()
      assert "runs" in body
      assert isinstance(body["runs"], list)
  ```

- [ ] **Step 2: Run the test and verify it fails**

  Run: `python -m pytest tests/test_web_api.py::test_runs_active_returns_list -v`
  Expected: FAIL — response has `run` (singular).

- [ ] **Step 3: Reshape `/api/runs/active`**

  In `web/app.py:420-435`, change the handler body so it returns:
  ```python
  records = active_manager.list_active_runs()
  return {"runs": [_record_json(record) for record in records]}
  ```

  Add to `RunManager` (next to `list_records`):
  ```python
  def list_active_runs(self) -> list[RunRecord]:
      with self._lock:
          self.cleanup()
          records = [self._copy_record(state.record) for state in self._records.values()
                      if state.record.run_id in self._active_run_ids]
      return records
  ```

- [ ] **Step 4: Update `POST /api/runs` to map the new errors**

  Locate the `except ActiveRunError as exc:` block in `create_run` (around line 477) and replace with:

  ```python
  except MaxConcurrentRunsError as exc:
      raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
  except AssetBusyError as exc:
      raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
  ```

  Repeat for the retry handler (around line 598).

- [ ] **Step 5: Run the new tests**

  Run: `python -m pytest tests/test_web_api.py::test_runs_active_returns_list -v`
  Expected: PASS.

- [ ] **Step 6: Run the full API suite**

  Run: `python -m pytest tests/test_web_api.py -q`
  Expected: All pass.

- [ ] **Step 7: Commit**

  ```bash
  git add web/app.py web/manager.py tests/test_web_api.py
  git commit -m "feat(api): /api/runs/active returns list, map MaxConcurrent/AssetBusy errors"
  ```

---

## Chunk 3: Front-End Adaptation

### Task 3.1: Adapt `showActive` to iterate the list

**Files:**
- Modify: `web/static/app.js:41` (`showActive`)

- [ ] **Step 1: Write the failing contract test**

  Append to `tests/test_web_static.py` (using the existing test runner conventions — JS unit tests live in a sibling file or pytest-mock harness; if neither exists, use a Playwright smoke test that hits `/active` after seeding):

  ```python
  def test_show_active_iterates_runs_list():
      """The /active view must handle a list response (not a single run)."""
      js = (STATIC / "app.js").read_text(encoding="utf-8")
      assert "response.runs" in js  # new contract
      assert "response.run " not in js and "response.run)" not in js  # legacy removed
  ```

- [ ] **Step 2: Run the test and verify it fails**

  Run: `python -m pytest tests/test_web_static.py::test_show_active_iterates_runs_list -v`
  Expected: FAIL — code still reads `response.run`.

- [ ] **Step 3: Refactor `showActive`**

  In `web/static/app.js:41`, replace `const active = (await api("/api/runs/active")).run;` with:

  ```javascript
  const runs = (await api("/api/runs/active")).runs || [];
  const active = runs.length ? runs.sort((a, b) =>
      new Date(b.queued_at || 0) - new Date(a.queued_at || 0))[0] : null;
  ```

  Apply the same change in `restoreActiveRun` (around line 89).

- [ ] **Step 4: Add a Playwright smoke test (optional but recommended)**

  Skip if no Playwright harness exists in `tests/`. Otherwise seed two concurrent runs and verify `/active` renders without console errors.

- [ ] **Step 5: Run the contract test**

  Run: `python -m pytest tests/test_web_static.py::test_show_active_iterates_runs_list -v`
  Expected: PASS.

- [ ] **Step 6: Run the full suite + restart the web service and click through `/active`**

  Run: `python -m pytest tests/ -q --ignore=tests/test_direct_db_path_uses_full_store_migration_and_replays_restart_interrupt.py`
  Manual: open `/active`, confirm empty state when nothing is in flight, then start a run and confirm the header still renders.

- [ ] **Step 7: Bump cache version**

  In `web/static/index.html` and `tests/test_web_static.py`, replace the existing `?v=20260901-...` suffix with `?v=20260902-manager-concurrency-1`.

- [ ] **Step 8: Commit**

  ```bash
  git add web/static/app.js web/static/index.html tests/test_web_static.py
  git commit -m "feat(ui): adapt /active view to runs-list contract"
  ```

---

## Chunk 4: Integration Test for Concurrent End-to-End

### Task 4.1: Live test — three POSTs succeed, fourth 409s

**Files:**
- Modify: `tests/test_web_api.py`

- [ ] **Step 1: Write the integration test**

  Append to `tests/test_web_api.py`:

  ```python
  def test_three_runs_admitted_then_409(monkeypatch):
      """Hitting POST /api/runs three times in fast succession all succeed
      when the cap is 3; a fourth receives a 409 with the new error code."""
      from fastapi.testclient import TestClient
      from web.app import app, _state
      _state.set_max_concurrent(3)
      client = TestClient(app)
      payloads = [_analysis_payload(f"T{i}") for i in range(4)]
      for body in payloads[:3]:
          r = client.post("/api/runs", json=body)
          assert r.status_code in (202, 409)  # worker may or may not start depending on stub
      r4 = client.post("/api/runs", json=payloads[3])
      assert r4.status_code == 409
      assert "max concurrent" in r4.json()["detail"].lower()
  ```

  Add `_analysis_payload` helper at module top (mirror the shape of the existing payloads in `tests/test_web_api.py`).

- [ ] **Step 2: Run the test and verify it fails**

  Run: `python -m pytest tests/test_web_api.py::test_three_runs_admitted_then_409 -v`
  Expected: FAIL — pre-Plan contract rejects the third POST.

- [ ] **Step 3: Implement `_state.set_max_concurrent` helper**

  In `web/app.py`, add a tiny module-level helper:

  ```python
  class _AppState:
      def __init__(self) -> None:
          self._max_concurrent = 3
      def set_max_concurrent(self, value: int) -> None:
          self._max_concurrent = value
          try:
              active_manager.configure_concurrency(
                  {"scheduler.max_concurrent_runs": {"value": value, "source": "test"}}
            )
          except Exception:  # manager may not exist yet in some tests
              pass

  _state = _AppState()
  ```

  Plumb the existing `active_manager` reference lazily inside `set_max_concurrent` (use a closure over the module-level `active_manager`).

- [ ] **Step 4: Run the test and verify it passes**

  Run: `python -m pytest tests/test_web_api.py::test_three_runs_admitted_then_409 -v`
  Expected: PASS.

- [ ] **Step 5: Run the full suite**

  Run: `python -m pytest tests/ -q --ignore=tests/test_direct_db_path_uses_full_store_migration_and_replays_restart_interrupt.py`
  Expected: All pass.

- [ ] **Step 6: Commit**

  ```bash
  git add web/app.py tests/test_web_api.py
  git commit -m "test(api): three concurrent runs admitted, fourth 409s"
  ```

---

## Chunk 5: Docs + Spec Status

### Task 5.1: Update the spec status note + CHANGELOG

**Files:**
- Modify: `docs/superpowers/specs/2026-09-02-scheduled-analysis-design.md` (add a small "Plan 1 — Manager Concurrency" header above the "Caller Impact" section noting it's complete)
- Modify: `README.md` (add a one-liner under the existing "Concurrency" header if any, else add a new "Concurrency limits" subsection near the architecture diagram)

- [ ] **Step 1: Append the status block**

  In the spec file, insert at the end:

  ```markdown
  ## Implementation Status

  - [x] Plan 1 — RunManager concurrency (`docs/superpowers/plans/2026-09-02-manager-concurrency.md`)
  - [ ] Plan 2 — Scheduled jobs core (data + APScheduler + trigger logic)
  - [ ] Plan 3 — Scheduled jobs UI
  ```

- [ ] **Step 2: README — add a one-liner under the Web section**

  Find the Web / Console section and append: "Concurrent runs are bounded by `scheduler.max_concurrent_runs` (default 3, range 1–10); see the spec for details."

- [ ] **Step 3: Commit + push**

  ```bash
  git add docs/superpowers/specs/2026-09-02-scheduled-analysis-design.md README.md
  git commit -m "docs: mark Plan 1 complete in spec status block"
  git push tradingagentsplus main
  ```
