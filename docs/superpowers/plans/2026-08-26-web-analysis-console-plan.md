# Web Analysis Console Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local FastAPI-based TradingAgents web console that runs one analysis at a time, streams safe progress to the browser, renders completed reports, and reopens reports from disk.

**Architecture:** Add a focused top-level `web` package with typed request/event models, a single-active-run manager, a graph runner adapter, safe history discovery, and a static browser client. Extend `TradingAgentsGraph.propagate` with optional streaming/cancellation callbacks while preserving existing callers and return values. Store web reports under an allowlisted `web_reports` directory and expose them through stable opaque IDs.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, Pydantic, `markdown-it-py`, `bleach`, SSE over `StreamingResponse`, vanilla HTML/CSS/JavaScript, pytest, FastAPI `TestClient`, and Playwright for the browser smoke test.

---

## Chunk 1: Core Streaming and Packaging

### Task 1: Define the public graph callback contract

**Files:**
- Create: `tests/test_graph_stream_callbacks.py`
- Modify: `tradingagents/graph/trading_graph.py`
- Modify: `tradingagents/graph/propagation.py`
- Modify: `tradingagents/graph/__init__.py` if the cancellation exception is re-exported

- [ ] **Step 1: Write the failing tests**

Add tests that patch a `TradingAgentsGraph` instance's graph and state helpers with a deterministic fake stream. Cover:

```python
def test_propagate_calls_on_chunk_and_keeps_return_shape():
    chunks = []
    result = graph.propagate("NVDA", "2026-08-26", on_chunk=chunks.append)
    assert chunks == expected_chunks
    assert isinstance(result, tuple) and len(result) == 2

def test_propagate_uses_existing_invoke_path_without_callbacks():
    graph.propagate("NVDA", "2026-08-26")
    assert fake_graph.invoke_called
    assert not fake_graph.stream_called

def test_debug_mode_without_callbacks_keeps_stream_and_merge_behavior():
    graph = make_graph(debug=True)
    graph.propagate("NVDA", "2026-08-26")
    assert fake_graph.stream_called
    assert not fake_graph.invoke_called

def test_should_cancel_only_forces_streaming_path():
    graph.propagate("NVDA", "2026-08-26", should_cancel=lambda: False)
    assert fake_graph.stream_called
    assert not fake_graph.invoke_called

def test_cancellation_stops_before_state_logging_and_memory_write():
    with pytest.raises(PropagationCancelled):
        graph.propagate("NVDA", "2026-08-26", should_cancel=lambda: True)
    assert graph.log_state_calls == 0
    assert graph.memory_store_calls == 0

def test_preflight_cancellation_skips_pending_resolution():
    with pytest.raises(PropagationCancelled):
        graph.propagate("NVDA", "2026-08-26", should_cancel=lambda: True)
    assert graph.pending_resolution_calls == 0

def test_cancellation_after_final_chunk_skips_side_effects():
    cancellation = iter([False, False, True])  # preflight, pre-chunk, post-final
    chunks = []
    with pytest.raises(PropagationCancelled):
        graph.propagate(
            "NVDA",
            "2026-08-26",
            on_chunk=chunks.append,
            should_cancel=lambda: next(cancellation),
        )
    assert chunks == expected_chunks
    assert graph.log_state_calls == 0
    assert graph.memory_store_calls == 0

def test_callback_exception_propagates_to_caller():
    with pytest.raises(RuntimeError, match="callback failure"):
        graph.propagate("NVDA", "2026-08-26", on_chunk=lambda _: raise_error())

def test_should_cancel_exception_propagates_to_caller():
    with pytest.raises(RuntimeError, match="cancel check failure"):
        graph.propagate("NVDA", "2026-08-26", should_cancel=lambda: raise_cancel_error())
```

Use existing fixtures and monkeypatch patterns from `tests/test_checkpoint_resume.py`; do not make network calls or construct real provider clients.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/test_graph_stream_callbacks.py -q`

Expected: FAIL because `propagate` does not accept callback arguments and `PropagationCancelled` does not exist.

- [ ] **Step 3: Implement the minimal callback/cancellation path**

Add a dedicated `PropagationCancelled(Exception)` in `tradingagents/graph/propagation.py`. Change `TradingAgentsGraph.propagate` and its private graph execution helper to accept keyword-only `on_chunk: Callable[[dict[str, Any]], None] | None = None` and `should_cancel: Callable[[], bool] | None = None` callbacks.

Call `should_cancel()` before `_resolve_pending_entries()` and before processing each streamed chunk, plus once after the final chunk. Preserve the current behavior exactly when both callbacks are `None`: debug mode uses the existing stream-and-merge behavior, non-debug mode uses `invoke()`. When either callback is supplied, use `graph.stream(init_agent_state, **args)`, call `on_chunk(chunk)` before merging it, and raise `PropagationCancelled` before `_log_state`, `memory_log.store_decision`, signal processing, or checkpoint cleanup for a cancelled run. Keep the existing `(final_state, signal)` return shape for successful runs. Do not catch callback exceptions, including exceptions raised by `should_cancel`.

- [ ] **Step 4: Run focused and regression tests**

Run: `pytest tests/test_graph_stream_callbacks.py tests/test_checkpoint_resume.py -q`

Expected: all focused tests pass; existing checkpoint behavior remains green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_graph_stream_callbacks.py tradingagents/graph/trading_graph.py tradingagents/graph/propagation.py tradingagents/graph/__init__.py
git commit -m "feat: expose graph streaming callbacks"
```

### Task 2: Add the web package and install surface

**Files:**
- Create: `web/__init__.py`
- Create: `web/models.py`
- Modify: `pyproject.toml`
- Modify: `cli/main.py`
- Create: `tests/test_web_models.py`
- Create: `tests/test_web_command.py`

- [ ] **Step 1: Write failing model and command tests**

Test that valid requests accept only `stock`/`crypto`, analyst keys `market`/`social`/`news`/`fundamentals`, and research depths `1`/`3`/`5`; invalid values return Pydantic validation errors. Test ticker input through `is_valid_ticker_input` and `normalize_ticker_symbol`, including whitespace/case normalization and rejection of unsafe characters; test strict `YYYY-MM-DD` parsing and rejection of impossible dates, duplicate analyst keys, and empty analyst lists. Test that crypto requests remove `fundamentals` using the existing CLI filtering rule and reject an empty effective analyst list for both asset types. Test that the Typer app exposes a `web` command which passes `host="127.0.0.1"` and the requested port to Uvicorn without requiring an API key during command setup.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_models.py tests/test_web_command.py -q`

Expected: FAIL because the `web` package, models, and command do not exist.

- [ ] **Step 3: Implement typed models and packaging**

Implement `web/models.py` with request, run-record, history, and event envelope models. Include explicit nullable fields and event payload discriminators matching the spec. Put normalization/effective-analyst logic in a small helper that delegates ticker normalization and crypto filtering to `cli.utils`.

Update `pyproject.toml`:

- add `fastapi`, `uvicorn`, `markdown-it-py>=3.0,<4.0`, and `bleach>=6.0,<7.0` to runtime dependencies;
- add a `web` optional extra containing `httpx` and `playwright`;
- include `web*` in package discovery;
- include `web/static/*` as package data;
- retain the existing CLI entry point.

Add a `web` Typer subcommand in `cli/main.py` that imports the app lazily and calls `uvicorn.run(app, host="127.0.0.1", port=port)`, with a `--port` option defaulting to `8000`.

- [ ] **Step 4: Verify models, command, and metadata**

Run: `pytest tests/test_web_models.py tests/test_web_command.py -q`

Run: `python -m pip wheel . --no-deps --no-build-isolation -w /tmp/tradingagents-web-wheel` and inspect the wheel with `unzip -l /tmp/tradingagents-web-wheel/*.whl`.

Expected: focused tests pass and the wheel listing contains `web/__init__.py`, `web/models.py`, and `web/static/index.html`.

- [ ] **Step 5: Commit**

```bash
git add web/__init__.py web/models.py pyproject.toml cli/main.py tests/test_web_models.py tests/test_web_command.py
git commit -m "feat: add web package and launch command"
```

---

## Chunk 2: Run Manager, Events, and Runner

### Task 3: Implement bounded run management and SSE event storage

**Files:**
- Create: `web/manager.py`
- Create: `tests/test_web_manager.py`

- [ ] **Step 1: Write failing manager tests**

Cover:

- creating one active run and returning HTTP-layer conflict information for a second active run;
- monotonically increasing `seq` values and ISO timestamps;
- independent subscriber cursors reading the same retained events;
- replay after `after_seq` and `Last-Event-ID` semantics;
- stale cursors receiving a `run_snapshot` before retained events;
- bounded event retention and one-hour terminal record expiry;
- idempotent cancellation of terminal records.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_manager.py -q`

Expected: FAIL because no manager implementation exists.

- [ ] **Step 3: Implement the manager**

Use a `ThreadPoolExecutor(max_workers=1)`, a lock, a condition variable per run, a bounded deque of events, and an active-run pointer. Store all fields required by the spec. `publish()` appends to the event log and notifies all subscribers without removing events. `read_events(run_id, cursor)` returns retained events in sequence order and indicates when a cursor predates retention. `wait_for_events()` uses a 15-second timeout so the API can emit SSE heartbeat comments. Use a monotonic event sequence per run and keep terminal records for one hour before cleanup.

Implement cancellation as a `threading.Event`. The manager owns lifecycle transitions, including an atomic `start_run()`/`begin_run()` transition from `queued` to `running` that sets `started_at`, appends `run_started`, and notifies subscribers; callers cannot directly mutate a run record. Error records accept only a stable code and sanitized message. Terminal manager APIs (`complete_run`, `fail_run`, `cancel_run`) must atomically update status/timestamps/metadata, release the active-run pointer, append the terminal event, and notify subscribers.

- [ ] **Step 4: Run focused tests and lint**

Run: `pytest tests/test_web_manager.py -q`

Run: `ruff check web tests/test_web_manager.py`

Expected: all manager tests pass and Ruff reports no violations.

- [ ] **Step 5: Commit**

```bash
git add web/manager.py tests/test_web_manager.py
git commit -m "feat: add bounded web run manager"
```

### Task 4: Adapt graph chunks into safe web events and reports

**Files:**
- Create: `web/runner.py`
- Create: `tests/test_web_runner.py`
- Modify: `tradingagents/graph/trading_graph.py` only if Task 1 needs a small integration correction

- [ ] **Step 1: Write failing runner tests**

Use a fake graph object and deterministic chunks to test:

- `run_started`, `phase_changed`, `agent_status`, `progress`, `message`, and `activity` emission;
- mapping analyst report fields, investment debate state, trader plan, and risk debate state to the correct phases and agents;
- redaction/shortening of tool activity;
- successful final signal and report sidecar creation;
- provider exceptions becoming `run_failed` with no traceback or secret values;
- `PropagationCancelled` becoming `run_cancelled` without report writing or memory-log storage.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_runner.py -q`

Expected: FAIL because the runner and event mapping do not exist.

- [ ] **Step 3: Implement the runner**

Create a runner that builds a fresh `DEFAULT_CONFIG` copy, applies request analyst/depth choices with the CLI's environment precedence, constructs `TradingAgentsGraph` with `debug=False`, and invokes `propagate(..., on_chunk=..., should_cancel=...)`.

Map known chunk keys using the same semantics as `cli.main.update_analyst_statuses`: analyst reports complete their agent, investment debate state transitions the research team, trader plan completes Trader, and risk debate state drives the three risk analysts and Portfolio Manager. Emit approximate progress at phase boundaries; unknown keys emit `activity_type="graph_update"`.

On success, call `save_reports` under `<results_dir>/web_reports/<safe_ticker>/<analysis_date>/<run_id>/`, write the required `run.json` sidecar with `report_id == run_id`, then call an atomic manager `complete_run()` API with the final signal and report ID. `complete_run()` must set status/timestamps/metadata, release the active-run pointer, append and publish `run_completed`, and notify all subscribers. On cancellation, call an atomic manager `cancel_run()` API that sets status/timestamps/phase/current-agent, releases the active-run pointer, and publishes `run_cancelled`; skip state logging, memory storage, and report writing. On other exceptions, log the full traceback server-side and call an atomic manager `fail_run()` API that stores only a stable sanitized error, releases the active-run pointer, and publishes `run_failed`. Terminal transitions must be idempotent and wake SSE subscribers.

- [ ] **Step 4: Run focused tests and lint**

Run: `pytest tests/test_web_runner.py tests/test_graph_stream_callbacks.py -q`

Run: `ruff check web tradingagents/graph/trading_graph.py`

Expected: focused tests pass and Ruff reports no violations.

- [ ] **Step 5: Commit**

```bash
git add web/runner.py tests/test_web_runner.py
git commit -m "feat: stream graph progress to web runs"
```

---

## Chunk 3: History and HTTP API

### Task 5: Implement safe report history discovery

**Files:**
- Create: `web/history.py`
- Create: `tests/test_web_history.py`

- [ ] **Step 1: Write failing history tests**

Create temporary result roots containing web reports with `run.json`, standard graph reports, CLI default reports, missing optional sections, and traversal-looking names. Test newest-first sorting and assert every list item exposes `report_id`, `source`, `ticker`, `generated_at`, and a bounded `decision_preview` derived from the portfolio decision when available. Test detail responses expose `complete_report` plus explicit section keys for `analysts.market`, `analysts.sentiment`, `analysts.news`, `analysts.fundamentals`, `research.bull`, `research.bear`, `research.manager`, `trading.trader`, `risk.aggressive`, `risk.conservative`, `risk.neutral`, and `portfolio.decision`, with missing values as empty strings. Also test sidecar metadata, legacy ID hashing, header/date parsing, missing metadata as `null`, and rejection of paths outside allowlisted roots.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_history.py -q`

Expected: FAIL because no history module exists.

- [ ] **Step 3: Implement history indexing and reads**

Implement exact allowlisted roots: `<results_dir>/web_reports`, `<results_dir>/reports`, and `<cwd>/reports`. Build an in-memory canonical ID index on each request or with an explicit refresh. Web IDs come from `run.json`; legacy IDs use `legacy-` plus the first 16 hex characters of the SHA-256 of a root-qualified relative identity such as `<source-root-name>/<relative-report-directory>`, preventing collisions across roots. If an ID still collides, append a deterministic `-2`, `-3`, etc. suffix in sorted root/path order. Never use a route parameter as a path.

Read `complete_report.md` and known section files. Parse the standard title/generated header and date-named parents for legacy metadata. Return empty section strings for missing optional files. Resolve paths and verify they remain descendants of their root before reading or downloading.

- [ ] **Step 4: Run focused tests and lint**

Run: `pytest tests/test_web_history.py -q`

Run: `ruff check web tests/test_web_history.py`

Expected: all history tests pass and Ruff reports no violations.

- [ ] **Step 5: Commit**

```bash
git add web/history.py tests/test_web_history.py
git commit -m "feat: expose safe report history"
```

### Task 6: Add FastAPI routes and SSE transport

**Files:**
- Create: `web/app.py`
- Create: `tests/test_web_api.py`
- Modify: `web/models.py` if route response models need clarification

- [ ] **Step 1: Write failing API tests**

Using FastAPI `TestClient` and a deterministic fake runner, cover:

- `GET /` and `GET /api/config` without secrets;
- `POST /api/runs` valid response `202` with opaque `run_id`, initial `queued` status, and effective analyst list; invalid input `422`, and active-run conflict `409`;
- `GET /api/config` includes supported asset types, analyst options, research depths, default date, output language, and provider name without secret values;
- `GET /api/runs/{id}` includes status, phase, current agent, progress, timestamps, request/effective analysts, and nullable final/error metadata, plus `404` for unknown IDs;
- `GET /api/runs/{id}` and `404` for unknown IDs;
- SSE retained replay, `Last-Event-ID`, `after_seq`, stale cursor snapshot, heartbeat, terminal stream closure, and event sequence ordering. Assert every emitted envelope includes `run_id`, integer `seq`, and ISO `timestamp`; assert each event payload includes its required fields from the event table, including nullable fields as JSON `null`. Assert stale replay sends `run_snapshot` before retained events, `Last-Event-ID` takes precedence over `after_seq`, and terminal closure occurs only after all retained events through the terminal sequence are sent;
- cancellation endpoint idempotency and `404` for unknown IDs on both `/events` and `/cancel`;
- history list/detail/download and traversal rejection. Assert history list metadata/decision preview, every explicit detail section key, `Content-Type: text/markdown` for downloads, a safe `Content-Disposition` attachment filename, and 404 for unknown opaque IDs.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_api.py -q`

Expected: FAIL because the FastAPI application and routes do not exist.

- [ ] **Step 3: Implement the FastAPI app**

Create an app factory that receives an optional manager/config for tests, mounts `web/static`, and serves `index.html` at `/`. Implement the explicit routes `GET /`, `GET /api/config`, `POST /api/runs`, `GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/events`, `POST /api/runs/{run_id}/cancel`, `GET /api/history`, `GET /api/history/{report_id}`, and `GET /api/history/{report_id}/download` with the response models named in `web/models.py`. Start worker submission only after request validation and effective analyst filtering. SSE uses `StreamingResponse` with `text/event-stream`, emits `id: <seq>` for every event so native `EventSource` reconnects carry `Last-Event-ID`, supports per-subscriber cursors, `Last-Event-ID` precedence over `after_seq`, snapshot fallback, 15-second comment heartbeats, and terminal closure after retained-event replay.

Use safe Markdown rendering through `markdown-it-py` followed by `bleach` with an explicit allowlist, or return escaped Markdown text if the implementation chooses the no-renderer fallback. Never include API keys, full environment values, prompts, raw provider payloads, or tracebacks.

- [ ] **Step 4: Run focused tests and lint**

Run: `pytest tests/test_web_api.py tests/test_web_history.py tests/test_web_manager.py -q`

Run: `ruff check web`

Expected: all API/history/manager tests pass and Ruff reports no violations.

- [ ] **Step 5: Commit**

```bash
git add web/app.py web/models.py tests/test_web_api.py
git commit -m "feat: add web API and SSE transport"
```

---

## Chunk 4: Browser UI and Verification

### Task 7: Build the Editorial Briefing browser client

**Files:**
- Create: `web/static/index.html`
- Create: `web/static/styles.css`
- Create: `web/static/app.js`
- Create: `tests/test_web_static.py`
- Create: `tests/test_web_browser.py`

- [ ] **Step 1: Write failing static/UI contract tests**

Test that the static shell contains the analysis form, running-state region, phase timeline, message feed, cancel action, report sections, history list, and download action. Test that JavaScript source references the documented API paths and event names and does not expose secret/config fields. Add a deterministic Playwright test harness that starts an app with a fake runner on an ephemeral localhost port and initially fails because the static shell and interactions do not exist.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_static.py tests/test_web_browser.py -q`

Expected: FAIL because the static files and browser interactions do not exist.

- [ ] **Step 3: Implement the UI**

Create a responsive Editorial Briefing interface with three client states: setup, running, and completed/failed/cancelled. The setup form loads `/api/config`, shows the local-mode header and provider/model summary without secrets, displays inline validation errors, disables fundamentals for crypto, submits `/api/runs`, and opens the SSE stream. Because browser `EventSource` cannot set `Last-Event-ID` manually, rely on native SSE `id:` handling for normal reconnects and use an explicit `?after_seq=<highestSeq>` fallback when the client deliberately recreates the connection; in both cases deduplicate by `seq` and refresh `GET /api/runs/{id}` after disconnects. The running state renders phase progress, current Agent, elapsed time, safe activity messages, and cancel. Failure/cancellation states preserve the stopped phase/current agent and show copyable sanitized diagnostics. Provide start-new and back-to-history actions.

The completed state renders sanitized report sections in exact workflow order (analysts, research, trading, risk, portfolio), displays signal and metadata, supports Markdown download, and refreshes recent history. During initial setup, fetch `/api/history` and populate the recent-runs list. Keep text inside stable containers, preserve readable contrast, and make the layout usable on both desktop and narrow screens without adding a build step or external asset dependency.

- [ ] **Step 4: Run static tests and browser smoke test**

Run: `pytest tests/test_web_static.py -q`

Run: `python -m pip install -e ".[dev,web]"` if the browser test dependencies are not installed.

Run: `python -m playwright install chromium` once in a developer environment if needed.

Run: `pytest tests/test_web_browser.py -q`

Expected: static contract tests and the deterministic Playwright flow pass. The harness uses temporary results/history output and explicit synchronization: one fake run emits `run_started`/phase events, waits until the cancel request, and emits `run_cancelled`; a second fake run raises a controlled provider error and emits `run_failed`; a third fake run emits the full phase sequence and terminal `run_completed`. The test verifies setup validation, initial history loading, provider summary redaction, actual SSE delivery and native `id:` reconnect behavior, running current-agent/phase rendering, failure/cancel diagnostics, completion report section order, start-new/back-to-history actions, history reopening, Markdown download content/filename, and desktop plus narrow viewport layouts.

- [ ] **Step 5: Commit**

```bash
git add web/static tests/test_web_static.py tests/test_web_browser.py
git commit -m "feat: add Editorial Briefing web UI"
```

### Task 8: Documentation, package smoke test, and full verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document local launch and behavior**

Add a README section showing `pip install .`, optional web test dependencies, `tradingagents web`, the `--port` override, the default loopback URL, and the fact that provider/API settings remain in `.env`/`DEFAULT_CONFIG`. Document that active runs are in-memory while completed reports persist on disk.

- [ ] **Step 2: Run the full verification suite**

Run: `ruff check .`

Run: `python -m pip install -e ".[dev,web]"`

Run: `python -m playwright install chromium`

Run: `pytest -q`

Run: `python -c "import tradingagents, cli.main, web.app; print('clean import OK')"`

Run:

```bash
wheel_dir=$(mktemp -d)
python -m pip wheel . --no-deps --no-build-isolation -w "$wheel_dir"
wheel_path=$(find "$wheel_dir" -maxdepth 1 -name '*.whl' -print -quit)
unzip -l "$wheel_path" | rg 'web/__init__.py|web/models.py|web/static/index.html'
venv_dir=$(mktemp -d)
python -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install "$wheel_path"
(cd /tmp && "$venv_dir/bin/python" -c "import web.app, tradingagents; print('isolated wheel import OK')")
"$venv_dir/bin/tradingagents" web --port 8765 > /tmp/tradingagents-web.log 2>&1 &
server_pid=$!
python - <<'PY'
import time
from urllib.request import urlopen

for _ in range(50):
    try:
        with urlopen("http://127.0.0.1:8765/", timeout=0.2) as response:
            assert response.status == 200
            assert b"TradingAgents" in response.read()
            break
    except Exception:
        time.sleep(0.1)
else:
    raise SystemExit("isolated tradingagents web did not serve /")
PY
kill "$server_pid"
```

Expected: Ruff passes, the full suite passes after installing `.[dev,web]` and Chromium, source-tree imports succeed, the wheel listing contains the web package/static shell, an isolated venv outside the repository imports the installed wheel, and the isolated venv's installed `tradingagents web` entry point serves `/` successfully on loopback.

- [ ] **Step 3: Start the server for manual verification**

Run: `tradingagents web --port 8000`

Expected: Uvicorn binds only to `127.0.0.1:8000`; opening `http://127.0.0.1:8000` shows the analysis form. Stop the server after verification.

- [ ] **Step 4: Commit documentation and final checks**

```bash
git add README.md
git commit -m "docs: document local web console"
```

After all tasks, run `git status --short --branch`, inspect the final diff, and use `superpowers:verification-before-completion` before claiming the feature is complete.
