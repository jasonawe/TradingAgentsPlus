# Investment Rating Localization Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show structured investment ratings as consistent Chinese-English labels throughout the Chinese web interface without changing persisted or downloaded report data.

**Architecture:** Add a small dependency-free browser/Node module that normalizes rating variants and returns plain display text. Load it before the existing application script, replace the watchlist-only mapping, and route the four structured rating surfaces through the shared formatter while retaining caller-specific empty-value fallbacks.

**Tech Stack:** Vanilla JavaScript, HTML, Node.js built-in test runner, pytest static/API tests.

---

## Chunk 1: Shared rating formatter and UI integration

### Task 1: Specify formatter behavior with failing tests

**Files:**
- Create: `web/static/rating-labels.test.js`
- Create: `web/static/rating-labels.js`

- [ ] **Step 1: Write the failing Node behavior test**

Create table-driven tests for all seven canonical ratings in Chinese and English. Add cases for uppercase, camel case, spaces, repeated whitespace, tabs, underscores, hyphens, unknown trimmed values, `null`, `undefined`, empty strings, and whitespace-only strings.

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `node --test web/static/rating-labels.test.js`

Expected: FAIL because `rating-labels.js` or `formatInvestmentRating` does not exist.

- [ ] **Step 3: Implement the minimal formatter module**

Expose a browser global and CommonJS export:

```javascript
function formatInvestmentRating(value, uiLanguage = "zh") {
  if (value == null) return null;
  const raw = String(value).trim();
  if (!raw) return null;
  const rating = RATING_LABELS[raw.toLowerCase().replace(/[\s_-]+/g, "")];
  if (!rating) return raw;
  return uiLanguage === "zh" ? `${rating.zh}（${rating.en}）` : rating.en;
}
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `node --test web/static/rating-labels.test.js`

Expected: all formatter cases PASS.

### Task 2: Integrate the formatter into every structured rating surface

**Files:**
- Modify: `web/static/index.html`
- Modify: `web/static/app.js`
- Modify: `tests/test_web_static.py`

- [ ] **Step 1: Write failing static integration assertions**

Assert that `index.html` loads `/static/rating-labels.js` before `/static/app.js`, and that the completion event, watchlist, history/report list, and report metadata call `formatInvestmentRating`. Assert Markdown rendering and the download URL remain separate from the formatter.

- [ ] **Step 2: Run the focused pytest test to verify it fails**

Run: `pytest -q tests/test_web_static.py`

Expected: FAIL because the new module is not loaded and the existing surfaces still use raw ratings or the old `signalLabel` mapping.

- [ ] **Step 3: Replace parallel mappings and wire callers**

Load the new module before `app.js`. Remove `signalLabel`. Add a small application wrapper that passes `state.language`, and use it at all four surfaces. Keep caller-specific fallbacks and continue passing all returned text through `escapeHtml` before HTML insertion.

- [ ] **Step 4: Bump the static asset version**

Update the query-string version for the modified scripts so an open browser reload does not reuse stale JavaScript.

- [ ] **Step 5: Run focused tests and syntax checks**

Run:

```bash
node --test web/static/rating-labels.test.js
node --check web/static/rating-labels.js
node --check web/static/app.js
pytest -q tests/test_web_static.py
```

Expected: all commands PASS.

### Task 3: Verify data and report regressions

**Files:**
- Test: `tests/test_web_api.py`
- Test: `tests/test_web_history.py`

- [ ] **Step 1: Run API and history regressions**

Run: `pytest -q tests/test_web_api.py tests/test_web_history.py`

Expected: PASS, confirming report API values, Markdown HTML rendering, and Markdown downloads remain unchanged.

- [ ] **Step 2: Run repository checks**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only the planned files and existing user-owned `.superpowers/` and `.ua/` paths appear.

- [ ] **Step 3: Commit and push the implementation**

Commit only the planned files with `feat: localize investment ratings`, then push `main` to the existing `tradingagentsplus` remote.

