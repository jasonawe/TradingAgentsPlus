# Watchlist Key Information Emphasis Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make watchlist price, movement, analysis time, and recommendation immediately scannable on desktop and mobile while preserving existing interactions.

**Architecture:** Keep `renderWatchlist()` as the display composition boundary and add explicit semantic markup/classes for price, currency, movement, analysis date, and recommendation. Scope the new visual semantics to watchlist selectors in `styles.css`; reuse existing translation, rating formatting, event hooks, and theme tokens.

**Tech Stack:** Vanilla JavaScript, CSS custom properties/media queries, pytest static contracts, optional Playwright smoke harness.

---

## Chunk 1: Rendering contracts and failing tests

### Task 1: Extend static watchlist contracts

**Files:**
- Modify: `tests/test_web_static.py` near existing watchlist contract tests
- Test: `tests/test_web_static.py`

- [ ] **Step 1: Write failing assertions**

Add `test_watchlist_key_information_markup_and_semantic_color_contract()` in `tests/test_web_static.py`. It must require `renderWatchlist()` to expose `.quote-price`, `.quote-currency`, `.quote-change`, `.is-up`, `.is-down`, `.is-flat`, the `aria-hidden` directional arrow, a localized latest-analysis label, and an explicit whitelist mapper covering `buy/strongbuy/overweight`, `sell/strongsell/underweight`, `hold`, missing, and unknown ratings. It must also assert `analysis_date` first with `generated_at` fallback, missing/invalid date handling, and CSS selectors scoped below `.watchlist-quote` and `.watchlist-analysis` so asset-detail classes are not changed accidentally.

- [ ] **Step 2: Run the focused test to verify RED**

Run: `pytest -q tests/test_web_static.py::test_watchlist_key_information_markup_and_semantic_color_contract`

Expected: FAIL because the current renderer has no semantic price/change classes or neutral movement contract.

### Task 2: Add focused browser fixture coverage

**Files:**
- Modify: `tests/test_web_browser.py` by extending the existing local browser flow test with a deterministic watchlist state matrix

- [ ] **Step 1: Add deterministic fixture assertions**

Extend the existing local browser flow fixture to render representative positive, negative, zero, missing, and non-finite movement values plus `buy`, `strongbuy`, `sell`, `strongsell`, `hold`, missing, and unknown ratings. Assert the expected scoped classes, explicit percentage/arrow text, latest-analysis date fallback, no horizontal overflow at 390px, light/dark semantic colors, and unchanged report/analyze/remove hooks. Keep this exact test opt-in under `TRADINGAGENTS_PLAYWRIGHT`.

- [ ] **Step 2: Run the browser test when available**

Run: `TRADINGAGENTS_PLAYWRIGHT=1 pytest -q tests/test_web_browser.py::test_real_browser_watchlist_key_information_hierarchy`

Expected: RED until the renderer and CSS are implemented; skip is acceptable when the optional Playwright dependency is unavailable.

## Chunk 2: Semantic watchlist renderer

### Task 3: Implement movement and recommendation class mapping

**Files:**
- Modify: `web/static/app.js:326-330`
- Test: `tests/test_web_static.py`

- [ ] **Step 1: Implement small display helpers inside the existing app module**

Normalize ratings by lowercasing and removing spaces, underscores, and hyphens. Map `buy`, `strongbuy`, and `overweight` to the concrete `is-buy` class; map `sell`, `strongsell`, and `underweight` to `is-sell`; map `hold` to `is-hold`; map missing analysis to `is-empty`; and map unknown values to `is-neutral` while preserving the formatted display label. For finite positive/negative changes emit an `aria-hidden` arrow plus explicit percentage text and classes `.is-up`/`.is-down`; treat zero as `.is-flat` and omit the badge for missing/non-finite values.

- [ ] **Step 2: Update the row markup**

Split the price into `.quote-price` and `.quote-currency`; render the movement as `.quote-change`; keep `.quote-meta` under `.watchlist-quote` for desktop; render `t("watchlist.latestAnalysisLabel")` plus a date and the recommendation chip under `.watchlist-analysis`. At 768px, `.watchlist-quote` and `.watchlist-analysis` become `display: contents` so child grid areas can place price, analysis date, quote metadata, and actions in the required order. The date helper must use `analysis.analysis_date` first, then the date portion of a valid `analysis.generated_at`; invalid or absent dates must render the existing localized `watchlist.noAnalysis` state. Preserve all existing data attributes and event binding hooks.

- [ ] **Step 3: Run the static contract test**

Run: `pytest -q tests/test_web_static.py::test_watchlist_key_information_markup_and_semantic_color_contract`

Expected: PASS.

## Chunk 3: Visual hierarchy, responsive behavior, and verification

### Task 4: Implement scoped light/dark watchlist styles

**Files:**
- Modify: `web/static/styles.css:216-250, 497-526`

- [ ] **Step 1: Style desktop hierarchy**

Increase price size and weight, keep currency small, style only `.watchlist-quote .quote-change.is-up` red, `.watchlist-quote .quote-change.is-down` green, and `.watchlist-quote .quote-change.is-flat` neutral with soft backgrounds. Give `.watchlist-analysis .signal-chip.is-buy`/`.is-sell` red/green treatments with `.is-hold`/`.is-neutral`/`.is-empty` neutral styling. Keep source/freshness metadata secondary, use existing dark-theme tokens, and leave asset-detail `.quote-up`/`.quote-down` rules unchanged.

- [ ] **Step 2: Add explicit 920px and 768px layout rules**

Retain the exact two-column intermediate grid areas `"asset quote" "analysis analysis" "actions actions"` at 920px. At 768px make `.watchlist-quote` and `.watchlist-analysis` `display: contents` and map `.watchlist-asset` to `asset`, `.watchlist-analysis .signal-chip` to `signal`, `.watchlist-quote .quote-value` to `quote`, `.watchlist-analysis .analysis-date` to `analysis-date`, `.watchlist-quote .quote-meta` to `quote-meta`, and `.watchlist-actions` to `actions` using areas `"asset signal" "quote quote" "analysis-date analysis-date" "quote-meta quote-meta" "actions actions"`. The recommendation chip is top-right beside identity, price/change follows, then analysis date and quote metadata remain readable before the actions row. Ensure long identity strings truncate and actions wrap without horizontal overflow.

- [ ] **Step 3: Verify static and syntax checks**

Run: `pytest -q tests/test_web_static.py`

Run: `node --check web/static/app.js`

Run: `git diff --check`

Expected: all pass.

### Task 5: Browser visual verification

**Files:**
- No production files; use the local app at `http://127.0.0.1:8766/`, started with `python -m uvicorn web.app:create_app --factory --host 127.0.0.1 --port 8766`

- [ ] **Step 1: Check populated desktop watchlist**

Verify price is the largest value, positive movement is red, negative movement is green, analysis date and recommendation are easy to locate, and report/analyze/remove controls remain usable.

- [ ] **Step 2: Check 390px mobile viewport**

Verify the stacked order, wrapped metadata, no horizontal overflow, and no clipped actions.

- [ ] **Step 3: Check dark theme**

Toggle the existing theme control and verify the same red/green semantics and readable contrast.

- [ ] **Step 4: Run focused regression suite**

Run: `TRADINGAGENTS_PLAYWRIGHT=1 pytest -q tests/test_web_static.py tests/test_web_browser.py::test_real_browser_watchlist_key_information_hierarchy`

Expected: static tests pass; browser tests pass when opt-in dependencies are enabled, otherwise only the documented skips remain.

- [ ] **Step 5: Capture verification evidence**

Run `mkdir -p /tmp/tradingagents-watchlist`, then save desktop and 390px screenshots from the browser pass to `/tmp/tradingagents-watchlist/desktop.png` and `/tmp/tradingagents-watchlist/mobile.png`; record the light/dark theme and horizontal-overflow assertions in the test output.
