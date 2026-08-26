# TradingAgents Web Analysis Console Design

**Date:** 2026-08-26

**Status:** Approved for implementation planning

## Goal

Add a useful local web interface for running TradingAgents analyses, following the existing CLI workflow while preserving the CLI as a supported entry point. The first release is a single-user, local analysis console: users configure an analysis, follow the live multi-agent run, read the completed briefing, and reopen reports saved by previous runs.

## Scope

### In scope

- A local FastAPI server bound to `127.0.0.1`.
- A static browser UI served by the FastAPI application without a frontend build toolchain.
- Analysis inputs: ticker, analysis date, asset type, selected analysts, and research depth.
- Reuse of `.env` and `DEFAULT_CONFIG` for model, provider, endpoint, language, data vendor, cache, and results-directory configuration.
- Live run progress: workflow phase, current agent, progress, status messages, and concise tool-call activity.
- Final report view with the existing analyst, research, trading, risk, and portfolio sections.
- History listing and reopening reports already stored on disk.
- Markdown report download.
- Cooperative cancellation of active runs.
- Automated API/unit tests and a browser smoke test.

### Out of scope for the first release

- User accounts, authentication, multi-user authorization, or remote deployment.
- Editing or storing API keys in the browser.
- A database or background job broker.
- Multiple simultaneous runs.
- Full prompt display, raw provider payloads, or unrestricted tool arguments.
- A second trading engine or a second report format.

## Existing-System Constraints

The existing application is a Python package whose public execution surface is `TradingAgentsGraph` in `tradingagents/graph/trading_graph.py`. Its workflow is a LangGraph `StateGraph` with streamed node chunks and a final state. The CLI already interprets chunks into analyst/team status, messages, and report sections. The web implementation should share those semantics rather than duplicate agent logic.

The existing report writer is `tradingagents/reporting.py`. Completed web runs should call `TradingAgentsGraph.save_reports` or the shared `write_report_tree` with an explicit web report directory. Existing CLI reports and existing state logs remain readable and are not migrated.

The configured results directory defaults to `~/.tradingagents/logs` and can be overridden by `TRADINGAGENTS_RESULTS_DIR`. A web run must use the resolved config value and must sanitize ticker-derived path components through the existing `safe_ticker_component` helper.

## User Experience

The visual direction is **Editorial Briefing** with a **running-focused layout**.

### Initial view

The initial view contains:

- A compact header identifying TradingAgents and the local running mode.
- A new-analysis form with ticker, date, asset type, analyst checkboxes, and research-depth selector.
- A small summary of the active provider/model configuration without displaying secrets.
- A recent-runs list that can open completed reports.
- Clear validation errors next to invalid inputs.

### Running view

After submission, the page changes to a running-focused view:

- A phase timeline: preparation, analyst team, research debate, trading plan, risk management, portfolio decision, and completion.
- A progress indicator that reflects the latest known phase and completion ratio.
- The current Agent and elapsed time.
- A bounded activity feed containing user-safe status messages and tool names, not full prompts or sensitive arguments.
- A cancel action while the run is active.
- A reconnect path: if SSE disconnects, the client fetches the run status and reconnects to the event stream.

### Completed view

On completion, the page changes to a readable editorial briefing:

- Ticker, analysis date, decision signal, and run metadata at the top.
- Report sections in workflow order: analyst reports, research team decision, trader plan, risk management, and portfolio manager decision.
- Markdown rendered safely as text/HTML according to the chosen client-side Markdown implementation; raw HTML from model output must not execute.
- A download button for `complete_report.md`.
- A link back to recent runs and a button to start a new analysis.

### Failure and cancellation views

Failures and cancellations keep the run metadata visible, identify the phase at which execution stopped, and show a concise, copyable diagnostic. API keys, authorization headers, and raw exception payloads must not be sent to the browser.

## Architecture

### Server package

Create a focused `web/` package:

- `web/app.py`: FastAPI application factory, static-file mounting, route registration, and server-facing configuration.
- `web/models.py`: Pydantic request/response models and event payload types.
- `web/manager.py`: single-active-run manager, in-memory run records, cancellation events, and thread-safe event queues.
- `web/runner.py`: adapter around `TradingAgentsGraph` that streams graph chunks, maps chunks into web events, writes reports, and returns a final run result.
- `web/history.py`: safe discovery and reading of report directories under the configured results directory.
- `web/static/index.html`: browser shell and semantic page regions.
- `web/static/styles.css`: Editorial Briefing visual system and responsive running/report layouts.
- `web/static/app.js`: form handling, SSE client, state rendering, history loading, reconnect behavior, cancellation, and report rendering.

The exact split may be adjusted in the implementation plan if an existing project convention suggests a smaller module, but each unit should retain one clear responsibility.

### Core integration

Extend the existing graph execution surface with an optional chunk callback, preserving all existing call signatures and behavior when the callback is omitted:

```python
graph.propagate(
    company_name,
    trade_date,
    asset_type="stock",
    on_chunk=web_runner.handle_chunk,
)
```

The callback is invoked for each streamed LangGraph chunk after it is received and before the final state is assembled. Debug pretty-printing remains controlled by the existing `debug` flag and must not be required for web progress.

The runner uses the existing `TradingAgentsGraph` lifecycle so instrument identity resolution, memory-log handling, checkpoint behavior, signal processing, and state logging stay centralized. It calls `save_reports` only after a successful final state is available.

### Task execution

The first release uses a `ThreadPoolExecutor` or equivalent bounded worker with one active run allowed. Each run receives a generated opaque `run_id`; it never uses an unsanitized ticker or user text as an identifier or path.

The manager stores, at minimum:

- `run_id`, ticker, analysis date, asset type, selected analysts, research depth
- lifecycle status: `queued`, `running`, `completed`, `failed`, or `cancelled`
- current phase and current agent
- progress value and timestamps
- bounded event history for reconnects
- final signal and report metadata when completed
- sanitized error summary when failed

The in-memory records are intentionally ephemeral. On process restart, active runs are gone; completed reports remain discoverable from disk.

### Event flow

1. The browser submits a validated run request.
2. The API creates a run record and starts the bounded worker.
3. The runner constructs `TradingAgentsGraph` with a copy of `DEFAULT_CONFIG`, applying only request-level choices that the current CLI already supports.
4. The runner invokes `propagate` with an `on_chunk` callback.
5. The callback maps graph state deltas and messages to small JSON events and puts them into the run's thread-safe queue.
6. The SSE endpoint drains queued events and emits `event:` plus JSON `data:` records.
7. The runner writes the report tree and publishes the final result event.
8. The browser renders the completed Editorial Briefing and refreshes the recent-runs list.

## API Contract

### `GET /`

Returns the static console shell.

### `GET /api/config`

Returns non-sensitive UI configuration, such as supported asset types, analyst options, research-depth options, default date, output language, and the active provider name. It must never include API key values, authorization headers, or full environment contents.

### `POST /api/runs`

Request:

```json
{
  "ticker": "NVDA",
  "analysis_date": "2026-08-26",
  "asset_type": "stock",
  "analysts": ["market", "social", "news", "fundamentals"],
  "research_depth": 1
}
```

Validation rules:

- ticker must pass the existing ticker normalization/validation rules;
- date must be `YYYY-MM-DD`;
- asset type must be a supported value;
- analyst keys must be known and at least one must be selected;
- research depth must be one of the existing supported values;
- a second active run returns HTTP 409.

Response: HTTP 202 with the new run ID and initial status.

### `GET /api/runs/{run_id}`

Returns the current run record, including status, phase, current agent, progress, timestamps, final signal/report metadata when present, and a sanitized error when applicable.

### `GET /api/runs/{run_id}/events`

Returns `text/event-stream`. Event names:

- `run_started`
- `phase_changed`
- `agent_status`
- `progress`
- `message`
- `activity`
- `run_completed`
- `run_failed`
- `run_cancelled`

Every event includes `run_id`, a monotonically increasing sequence number, and an ISO timestamp. Event history is bounded; the endpoint sends retained events first, then waits for new events. A terminal event closes the stream.

### `POST /api/runs/{run_id}/cancel`

Sets the cooperative cancellation flag and returns the current run record. It is idempotent for terminal runs. The runner checks the flag between graph chunks and emits `run_cancelled` when it stops.

### `GET /api/history`

Lists report directories containing `complete_report.md` under the configured web report root and compatible existing report roots. Results are sorted newest first and include run ID/path, ticker, generated timestamp, and a short decision preview when available. Directory traversal must remain inside the configured results directory.

### `GET /api/history/{run_id}`

Returns the complete report plus known section files as structured JSON. Missing optional sections are represented as empty values rather than errors.

### `GET /api/history/{run_id}/download`

Streams the corresponding `complete_report.md` with a download filename. The route must reject path traversal and unknown report IDs.

## Progress Mapping

The runner maps existing graph chunks to stable phase and agent labels. The initial implementation should reuse the CLI's established interpretation for analyst reports, investment debate state, trader plan, and risk debate state. Unknown or future graph fields become a generic activity event rather than causing the run to fail.

Progress is deliberately approximate. It should advance at phase boundaries and use the configured analyst count to distribute the analyst phase; it must not claim token-level or provider-level completion that the graph cannot measure.

Tool events include the tool name and a redacted/short argument summary only when the summary can be generated without exposing secrets. Full tool arguments and model prompts are never streamed.

## Persistence and History

Web reports use an explicit directory below the configured results directory, for example:

```text
<results_dir>/web_reports/<safe_ticker>/<analysis_date>/<run_id>/
  complete_report.md
  1_analysts/*.md
  2_research/*.md
  3_trading/*.md
  4_risk/*.md
  5_portfolio/*.md
```

The exact directory layout should remain compatible with `write_report_tree`. The opaque `run_id` prevents collisions when the same ticker/date is analyzed repeatedly. Existing CLI reports are read-only history inputs; the web UI does not rewrite them.

## Error Handling and Security

- Bind the development server to loopback by default.
- Validate and normalize ticker values before constructing paths or graph state.
- Resolve report paths and verify they remain descendants of the configured results root.
- Do not send environment variables, API keys, request headers, raw provider responses, or complete exception tracebacks to clients.
- Catch graph/provider exceptions in the worker, log the full traceback server-side, and publish a concise stable error code/message.
- Treat model-generated Markdown as untrusted content; sanitize or render it through a safe Markdown path.
- Return HTTP 404 for unknown run/history IDs and HTTP 409 for an active-run conflict.
- A cancelled run must not be reported as completed and must not write a misleading final decision.
- Queue and event history sizes are bounded to avoid unbounded memory growth from long runs.

## Testing Strategy

### Unit/API tests

- Request model validation for ticker, date, asset type, analysts, and research depth.
- Single-active-run enforcement and terminal-state idempotency.
- Run manager transitions for started, phase change, completed, failed, and cancelled states.
- SSE event ordering, sequence numbers, terminal closure, and reconnect behavior.
- Chunk-to-event mapping for analyst, research, trading, risk, and final decision chunks.
- Cooperative cancellation between streamed chunks.
- History discovery, sorting, missing-section handling, path traversal rejection, and download content type.
- Redaction guarantees for errors and tool activity.
- Core graph callback compatibility: existing `propagate` behavior remains unchanged without a callback.

### Browser smoke test

Use Playwright against a test-configured app with the graph runner replaced by a deterministic fake. Cover:

1. Load the console and see the analysis form.
2. Submit valid inputs and see the running view.
3. Observe phase/current-agent updates through SSE.
4. Cancel a run and see the cancelled state.
5. Complete a run and see the Editorial Briefing with section content.
6. Refresh/open a history item and download the Markdown report.

### Existing checks

Run the existing Ruff check and test suite after dependencies are installed. The web feature must not require API keys or external provider calls in unit/browser tests.

## Acceptance Criteria

- A user can launch the local server and open a browser page without a frontend build step.
- A valid analysis can be submitted using existing `.env`/`DEFAULT_CONFIG` provider settings.
- The browser receives visible phase, agent, and message updates while the graph runs.
- A completed analysis writes a standard report tree and is immediately readable in the browser.
- A previous report can be reopened and downloaded after restarting the server.
- A second concurrent run is rejected clearly.
- Cancellation, provider failure, invalid input, and missing history records produce explicit, non-sensitive UI states.
- Existing CLI behavior and public `TradingAgentsGraph` callers remain compatible.
- Ruff and the focused web test suite pass; the full suite passes in an environment with project dependencies installed.
