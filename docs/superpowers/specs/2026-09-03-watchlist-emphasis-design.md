# 2026-09-03 — Watchlist Key Information Emphasis Design

**Date:** 2026-09-03
**Scope:** Improve the visual hierarchy of the watchlist asset rows for price,
change percentage, latest analysis time, and recommendation.

## Background

The watchlist already renders live quote data and the latest completed analysis,
but the row currently gives similar visual weight to identity, quote metadata,
analysis metadata, and actions. A user scanning several assets cannot quickly
identify the current price, daily movement, or latest recommendation. The
existing quote classes also use the generic green-for-up/red-for-down convention,
which conflicts with the requested Chinese-market convention of red for gains
and green for losses.

## Goal

- Make price the strongest numeric signal in each watchlist row.
- Put the change percentage immediately beside the price with a high-contrast
  directional treatment: red for positive movement, green for negative movement.
- Make the latest analysis date easy to find without competing with the quote.
- Make recommendations visually scannable: bullish recommendations use red,
  bearish recommendations use green, and neutral or missing recommendations use
  a neutral treatment.
- Preserve existing row navigation, report links, analysis actions, removal
  confirmation, quote freshness text, translations, and dark-theme support.
- Keep the layout usable on desktop and mobile without horizontal overflow.

## Non-Goals

- Changing quote providers, quote calculations, polling cadence, or API shapes.
- Reclassifying the meaning of analysis ratings or changing report content.
- Redesigning the watchlist form, sidebar, topbar, or unrelated history/library
  components.

## Design

### Desktop row

Each `.watchlist-row` remains a single interactive row, but the columns become
four information groups plus actions:

1. **Asset identity:** ticker is bold monospace, followed by name/exchange and
   asset type in secondary text.
2. **Quote focus:** price is the largest text in the row. Currency remains a
   small suffix. Change percentage is an adjacent high-contrast badge with a
   directional arrow. Quote source, freshness, and quote time remain below as
   secondary metadata.
3. **Analysis signal:** a small "最近分析" label precedes the analysis date.
   The recommendation chip sits directly below the date and uses the requested
   market color semantics.
4. **Actions:** existing start-analysis, view-report, and remove controls remain
   available and retain their current click behavior.

The row uses the existing panel, line, typography, radius, and shadow tokens.
It receives a subtle panel surface and hover state so the stronger hierarchy
does not rely on color alone.

### Mobile row

At `max-width: 920px`, the row switches to a two-column layout with identity and
quote on the first line and analysis spanning the second line; actions remain
in the final available column. At `max-width: 768px`, it becomes a fully
stacked layout with explicit order:

1. ticker and asset identity, with the recommendation chip aligned to the top
   right when available;
2. price, currency, and change badge on one line when space permits;
3. analysis date and quote freshness metadata as wrapping secondary text;
4. existing actions on a separate wrapping row.

The 769–920px layout keeps the same semantic order but may place actions beside
analysis when width allows. The 390px acceptance viewport must use the fully
stacked layout. Grid areas/order are defined in CSS rather than relying on
source-order changes, so existing event hooks remain stable. Long names and
metadata truncate or wrap within their own group. No fixed-width element may
force horizontal scrolling.

### Color semantics

The watchlist-specific quote classes are scoped under `.watchlist-quote`; the
generic `.quote-up`/`.quote-down` classes used by the asset detail view are not
reused for this semantic mapping. The render contract adds these classes:

- `.quote-price` for the numeric price;
- `.quote-currency` for the currency suffix;
- `.quote-change` plus one of `.is-up`, `.is-down`, or `.is-flat` for movement.

The quote movement classes are:

- `.watchlist-quote .quote-change.is-up`: `--red` / `--red-soft` for finite
  positive movement;
- `.watchlist-quote .quote-change.is-down`: `--green` / `--green-soft` for
  finite negative movement;
- `.watchlist-quote .quote-change.is-flat`: neutral text/surface, no arrow, for
  zero movement. Missing, non-numeric, or non-finite values omit the badge;
  explicit numeric zero may render a neutral `0.00%` badge.

The directional arrow is text markup inside the badge (`↑` for up, `↓` for
down), marked `aria-hidden="true"`; the explicit percentage remains visible so
color and icon are never the only signal.

Recommendation classes are normalized by lowercasing and removing spaces,
underscores, and hyphens, then mapping through an explicit whitelist:

- `buy`, `strongbuy`, and `overweight`: red bullish treatment;
- `sell`, `strongsell`, and `underweight`: green bearish treatment;
- `hold` and missing analysis: neutral treatment;
- unknown values: neutral styling while preserving the formatted display label.

The semantic mapping is scoped to the watchlist so existing global status and
asset-detail conventions are not unintentionally changed. Dark mode uses the
same semantic mapping with the existing dark-theme tokens and sufficient text
contrast.

## Data Flow and Boundaries

- `renderWatchlist()` remains the single UI composition boundary. It derives
  display-only classes and labels from the existing `quote.change_percent` and
  latest analysis fields; no API or persistence changes are needed. The
  analysis block uses a localized `watchlist.latestAnalysis` label and renders
  `analysis_date` first, falling back to the date portion of `generated_at`.
  Invalid or absent dates render the localized missing-analysis state without a
  fabricated timestamp.
- `latestAnalysisFor()` remains responsible for selecting the latest completed
  analysis. The rendering layer only formats its date and rating.
- CSS owns hierarchy, spacing, responsive layout, and color treatment. JavaScript
  owns only semantic class selection and accessible text/labels.
- Existing event binding continues to target the same `.watchlist-row`,
  `[data-report-id]`, `[data-analyze-symbol]`, and `[data-remove-watchlist]`
  hooks.

## Edge Cases and Error Handling

- Missing price keeps the existing "no quote" text and neutral styling.
- Missing change percentage omits the badge without leaving an empty visual
  container.
- Missing analysis keeps the existing "暂无分析报告" chip with neutral styling.
- Unknown rating values fall back to neutral recommendation styling while
  preserving the formatted label.
- `latestAnalysisFor()` keeps its current compatibility behavior: it selects
  completed records plus legacy/no-status records, and the rendering layer does
  not broaden or narrow that selection as part of this visual change.
- Long ticker/name/currency strings must remain clipped or wrap within the row;
  they must not overlap actions or create horizontal page overflow.
- Quote errors continue to show the existing localized error/source text.

## Accessibility

- Directional arrows are supplementary; the percentage text remains explicit
  so color is not the only signal.
- Existing button labels and remove-button aria labels remain unchanged.
- Focus styles and row click behavior remain intact.
- Contrast must be checked in both light and dark themes for quote badges and
  recommendation chips.

## Testing and Verification

1. Extend `tests/test_web_static.py` to assert `.quote-price`,
   `.quote-currency`, `.quote-change`, the scoped movement classes, the
   localized analysis label, and the whitelisted recommendation class mapper.
2. Add focused browser checks in `tests/test_web_watchlist_ui.py` (or the
   existing opt-in browser test module) with deterministic fixtures for positive,
   negative, zero, missing, and non-finite movement values; `buy`, `strongbuy`,
   `sell`, `strongsell`, `hold`, missing, and unknown ratings; 390px width; and
   light/dark themes. Assert no horizontal overflow and unchanged row/report/
   analyze/remove hooks.
3. Run the existing web static test module and the focused scheduled/watchlist
   regression tests.
4. Run a desktop and mobile screenshot pass to verify no overlap, clipped
   actions, or horizontal overflow.

## Acceptance Criteria

- In a populated watchlist, price is visibly larger than identity and metadata.
- Positive movement is red and negative movement is green in the watchlist.
- Latest analysis date and recommendation can be located without reading the
  full row.
- Rows remain navigable and all existing actions work.
- Mobile layout is readable at 390px viewport width with no horizontal scroll.
- Light and dark themes preserve the same semantic color mapping and readable
  contrast.
