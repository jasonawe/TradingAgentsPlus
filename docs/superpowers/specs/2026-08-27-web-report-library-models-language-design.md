# TradingAgents Web Report Library, Model Selection, and Output Languages

**Date:** 2026-08-27

**Status:** Approved for implementation planning

## Goal

Extend the local TradingAgents web console so users can browse and read prior reports, choose an available provider/model configuration for a new run, and choose the language used by generated reports. The console remains local, single-user, and safe by keeping credentials and endpoints outside the browser.

## Scope

### In scope

- A report library view that lists reports discovered by the existing safe history index.
- Search, asset-type filtering, signal/status filtering, and newest/oldest sorting for report history.
- A reusable report detail view for both newly completed runs and archived reports.
- Markdown download from the report detail view.
- Provider, quick-thinking model, and deep-thinking model selectors populated from `tradingagents.llm_clients.model_catalog`.
- Validation that submitted provider/model pairs are present in the server-side catalog.
- Output-language selection for the languages already supported by the CLI.
- Run metadata that records the selected provider, models, and output language without recording secrets.
- English/Simplified Chinese localization of all new controls and states.
- API, unit, static-contract, and browser smoke coverage for the new behavior.

### Out of scope

- Editing API keys, endpoints, proxy settings, or other secrets in the browser.
- Adding new providers or models to the catalog.
- Translating report content after it has been generated.
- User accounts, remote deployment, multiple concurrent runs, or a database-backed history index.

## Existing-System Constraints

The model catalog in `tradingagents/llm_clients/model_catalog.py` is the source of truth for provider/mode/model combinations. The web API must import and expose a safe projection of this catalog rather than maintaining a second model list. The current configuration comes from `DEFAULT_CONFIG` and environment overrides; those values are the defaults shown in the form.

The Web selector exposes only providers that have at least one fixed, non-`custom` model in both quick and deep catalog modes: `openai`, `anthropic`, `google`, `xai`, `deepseek`, `qwen`, `qwen-cn`, `glm`, `glm-cn`, `minimax`, `minimax-cn`, and `ollama`. Providers whose catalog is custom-only (`openrouter`, `azure`, `openai_compatible`, `mistral`, `kimi`, `groq`, `nvidia`, and `bedrock`) are not shown by the Web selector in this release. This preserves the selected-model-only scope; the CLI continues to support those providers independently.

The current `ReportHistory` index discovers web and compatible legacy reports under allowlisted roots. The report library must call this service and must not expose arbitrary filesystem paths. Existing report section files and `complete_report.md` remain the source content.

The current web runner copies configuration before constructing `TradingAgentsGraph`. Request-level provider/model/language choices may override only those corresponding configuration keys. API keys and endpoints remain sourced from the process environment/configuration and are never accepted from the browser. Regional provider keys (`qwen-cn`, `glm-cn`, and `minimax-cn`) retain the endpoint selected by the existing config/CLI; an explicitly configured `backend_url` remains authoritative, and the Web request never supplies an endpoint.

## User Experience

### Report library

The setup screen gains a prominent report-library action and retains a compact recent-report preview. The report library contains:

- a search input matching ticker and report preview text;
- asset type and signal/status filter controls;
- sort control for newest or oldest first;
- a list showing ticker, analysis date, signal/status, generated time, source, and a short decision preview;
- an empty state, loading state, and unavailable state in both interface languages.

Selecting a report opens the report detail view. The detail view shows metadata, workflow sections, safe Markdown rendering, download, back-to-library, and new-analysis actions. On narrow screens, the list and detail are sequential views with a back action; no horizontal scrolling is required.

### Model and language controls

The analysis form adds:

- Provider selector;
- Quick model selector;
- Deep model selector;
- Analysis output language selector.

Changing provider refreshes both model selectors from the matching catalog entries. The current configured values are selected when available; otherwise the first catalog entry is selected and the user sees a non-secret configuration summary. The form submits provider, quick model, deep model, and output language with the run request.

The browser interface language remains a separate English/Simplified Chinese preference. Switching it translates controls and status text but does not silently change a previously selected output language; the output-language selector displays its own explicit value. The Web output-language allowlist is exactly the eleven fixed CLI choices: `English`, `Chinese`, `Japanese`, `Korean`, `Hindi`, `Spanish`, `Portuguese`, `French`, `German`, `Arabic`, and `Russian`. Custom free-text languages are not accepted by the Web API in this release.

## Architecture

### Server changes

- Extend `GET /api/config` with a catalog projection:
  - providers and display keys;
  - per-provider quick/deep model values and labels;
  - configured provider, quick model, deep model, and output language;
  - the complete eleven-item supported output-language list.
- Extend `AnalysisRequest` with optional provider, quick model, deep model, and output language fields. Omitted fields resolve from the active configured values; if a configured value is not in the Web catalog, the server uses the first valid catalog option for that provider/mode. A submitted partial model selection is completed from the selected provider's configured/default counterpart. Values are normalized and checked against the model catalog and language allowlist.
- Extend run metadata and sidecar JSON with selected provider, quick model, deep model, and output language.
- Extend history summaries and details with `asset_type`, `status`, provider, models, and output language when present in the sidecar; legacy reports without those fields return `null` and are treated as `Unknown` by the UI filters.
- Apply validated request choices to the copied runner config before graph construction.
- Keep API error responses generic/sanitized; do not echo credentials or endpoint values.

### Browser changes

- Add an explicit library view and report detail view while preserving the live-run view.
- Store the config catalog in client state and render dependent model selectors from it.
- Add report filtering/sorting as local derived state over `/api/history` results; refresh remains available.
- Reuse `renderReport` for live-completed and archived reports.
- Add dictionary keys for all new controls, filters, metadata, statuses, and output languages.
- Escape all report-derived text before insertion and keep the existing conservative Markdown renderer.

## Data Flow

1. Browser requests `/api/config` and `/api/history` on startup.
2. API returns the safe catalog projection and report summaries.
3. Browser selects provider; dependent quick/deep model options update from the catalog.
4. Browser submits validated choices and output language in `POST /api/runs`.
5. Pydantic validates the request, including provider/model catalog membership and language allowlist membership.
6. Runner copies the active config and applies the validated request choices, then constructs `TradingAgentsGraph`.
7. Runner writes report files and sidecar metadata containing non-secret run choices.
8. Browser receives completion, loads the report detail, and can later reopen it through the report library.

## API Contract

### `GET /api/config`

Response shape:

```json
{
  "providers": [
    {
      "key": "openai",
      "label": "OpenAI",
      "quick_models": [{"id": "gpt-5.4-mini", "label": "GPT-5.4 Mini"}],
      "deep_models": [{"id": "gpt-5.5", "label": "GPT-5.5"}]
    }
  ],
  "configured": {
    "provider": "openai",
    "quick_model": "gpt-5.4-mini",
    "deep_model": "gpt-5.5",
    "output_language": "English"
  },
  "output_languages": ["English", "Chinese", "Japanese", "Korean", "Hindi", "Spanish", "Portuguese", "French", "German", "Arabic", "Russian"],
  "supported_asset_types": ["stock", "crypto"],
  "analyst_options": [],
  "research_depths": [1, 3, 5],
  "default_date": "2026-08-27"
}
```

Labels may be localized client-side from stable provider/model IDs. The response must not contain key names, key values, authorization headers, or full environment contents.

### `POST /api/runs`

The request adds:

```json
{
  "provider": "openai",
  "quick_model": "gpt-5.4-mini",
  "deep_model": "gpt-5.5",
  "output_language": "Chinese"
}
```

Invalid provider/model combinations or unsupported languages return HTTP 422. A valid request retains the existing ticker, date, asset, analyst, depth, and single-active-run validation. For backward compatibility, omitting the new fields uses the active configured provider/models/language after the fallback rules above.

### Run/history metadata

`GET /api/runs/{run_id}`, history summaries, report details, and `run.json` may include provider, quick model, deep model, and output language. These values are user-selected identifiers only and must never include credentials or endpoint URLs. History fields unavailable in a legacy sidecar are `null`.

## Validation and Error Handling

- Provider keys must exist in the Web-supported fixed-model provider set.
- Quick and deep model IDs must exist in the selected provider's corresponding non-`custom` mode list; catalog entries marked custom are not exposed as a free-text field in this release.
- Output language must be one of the eleven fixed CLI language choices; custom free-text language is rejected by the Web API.
- If a configured provider or model is no longer present in the Web catalog, the API exposes the first valid provider/model combination and the UI selects it rather than submitting an invalid pair. Omitted request fields follow the same resolution.
- Regional provider endpoint selection follows the existing process configuration; a configured `backend_url` takes precedence over any provider default and no endpoint is accepted in request JSON.
- Validation failures return a stable sanitized shape such as `{ "detail": "invalid analysis configuration" }`; rejected values are not echoed and FastAPI/Pydantic validation details are normalized before reaching the browser.
- History search/filtering is client-side and fails open to the existing history-unavailable state if the API cannot be reached.
- Report content remains escaped/safely rendered; model-generated HTML never executes. If workflow section files are missing, the detail view renders the escaped `complete_report` fallback instead of a blank report.

## Testing

- Model catalog projection tests verify every exposed provider has quick/deep entries and no secret/config values leak.
- Request model tests cover valid selections, invalid provider/model pairs, unsupported languages, backward-compatible defaults, and sanitized validation responses.
- Runner tests verify request choices reach the graph config and sidecar metadata.
- History/API tests verify report-library list data and report details remain safe.
- History tests verify asset/status/model/language metadata and null behavior for legacy sidecars.
- Static contract tests verify report-library controls, dependent model rendering, output language controls, and bilingual dictionary coverage.
- Browser smoke tests verify:
  - report list renders and filters/sorts;
  - provider changes update both model selectors;
  - English/Simplified Chinese UI switching works;
  - output language selection is submitted independently;
  - archived report detail opens on desktop and mobile without overflow.
  - archived reports without section files still render their complete report fallback.
