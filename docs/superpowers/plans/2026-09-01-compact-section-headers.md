# Compact Section Headers Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove redundant large page headers while preserving compact content headings, actions, routes, and accessible page names.

**Architecture:** Keep the existing single-page view structure and JavaScript IDs. Change only static HTML hierarchy and CSS presentation; use visually hidden `h1` elements for page semantics and compact toolbars for actions.

**Tech Stack:** Static HTML, CSS, Python pytest string-contract tests, local browser verification.

---

## Chunk 1: Compact Page Hierarchy

### Task 1: Lock the intended markup and typography contract

**Files:**
- Modify: `tests/test_web_static.py`

- [x] Add a test asserting page-level headings are visually hidden, obsolete promotional header wrappers are removed, and content/object titles use compact CSS.
- [x] Run the targeted test and confirm it fails against the current markup.

### Task 2: Simplify static page headers

**Files:**
- Modify: `web/static/index.html`
- Modify: `web/static/styles.css`

- [x] Replace visible page introductions with visually hidden semantic headings.
- [x] Move refresh and new-analysis actions into compact action toolbars.
- [x] Reduce content, run, and report heading sizes without changing element IDs used by JavaScript.
- [x] Run the targeted static test and full static regression suite.

### Task 3: Verify the rendered routes

**Files:**
- No source changes expected.

- [ ] Restart the local web service.
- [x] Inspect `/`, `/analysis`, `/active`, `/reports`, `/settings`, and a report route at desktop and mobile widths.
- [x] Confirm there are no blank header gaps, overlaps, missing controls, or broken navigation.
- [x] Run `git diff --check`, commit, and push the scoped change.
