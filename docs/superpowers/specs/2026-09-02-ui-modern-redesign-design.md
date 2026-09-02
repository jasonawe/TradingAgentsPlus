# 2026-09-02 — UI Modern Redesign (Direction C)

## Goal

Replace the editorial/rust-accent aesthetic across the entire TradingAgents
web console with a Linear/Vercel-style modern SaaS interface: clean white
panels, soft elevation, indigo accent, generous padding, monospace data.

Picked direction: **C — Modern SaaS** (user-confirmed, full-scope).

## Scope

Every view in the console, plus system-level primitives:

- Watchlist (setup / landing)
- Asset detail (`/assets/{symbol}`)
- New Analysis form
- Active jobs (`/active`)
- Report detail (`/reports/{id}`)
- Library (`/reports`)
- Settings (`/settings`)
- Modal dialogs (delete confirm, etc.)
- Empty / loading / error states

System primitives:

- Design tokens (color, spacing, radius, shadow, typography)
- Light + dark themes (toggle in topbar, persisted in localStorage)
- Icon system (inline SVG, no CDN)
- Responsive layout (sidebar collapses below 768px)

## Design Tokens

```css
:root {
  /* surfaces */
  --bg:      #fafafa;
  --panel:   #ffffff;
  --line:    #e6e6e6;
  --line-strong: #d4d4d4;

  /* ink */
  --ink:     #0a0a0a;
  --ink-2:   #262626;
  --muted:   #737373;
  --subtle:  #a3a3a3;

  /* brand */
  --accent:        #5b21b6;       /* indigo-800 */
  --accent-hover:  #4c1d95;
  --accent-soft:   rgba(91,33,182,.08);

  /* data */
  --green:  #16a34a;
  --green-soft: rgba(22,163,74,.1);
  --red:    #dc2626;
  --red-soft:   rgba(220,38,38,.1);
  --amber:  #d97706;

  /* radius */
  --r-sm: 6px;   /* tags */
  --r-md: 8px;   /* buttons, inputs */
  --r-lg: 12px;  /* cards */

  /* shadow */
  --shadow-1: 0 1px 2px rgba(0,0,0,.04);
  --shadow-2: 0 1px 3px rgba(0,0,0,.04), 0 4px 12px rgba(0,0,0,.04);

  /* font */
  --sans:  'Inter', system-ui, -apple-system, sans-serif;
  --mono:  ui-monospace, 'SF Mono', Menlo, monospace;
}

[data-theme="dark"] {
  --bg:      #0a0a0a;
  --panel:   #171717;
  --line:    #262626;
  --line-strong: #404040;
  --ink:     #fafafa;
  --ink-2:   #e5e5e5;
  --muted:   #a3a3a3;
  --subtle:  #737373;
  --accent:        #8b5cf6;
  --accent-hover:  #a78bfa;
  --accent-soft:   rgba(139,92,246,.15);
  --shadow-1: 0 1px 2px rgba(0,0,0,.4);
  --shadow-2: 0 1px 3px rgba(0,0,0,.4), 0 4px 12px rgba(0,0,0,.3);
}
```

## Layout

```
┌─────────────────────────────────────────────────────────┐
│ ┌─────────┐ ┌────────────────────────────────────────┐ │
│ │ sidebar │ │  topbar (search · theme · actions)    │ │
│ │  260px  │ ├────────────────────────────────────────┤ │
│ │         │ │                                        │ │
│ │ brand   │ │  main content (max-width 1200px)       │ │
│ │ nav     │ │                                        │ │
│ │ watch+  │ │                                        │ │
│ │         │ │                                        │ │
│ │ ──      │ │                                        │ │
│ │ user    │ │                                        │ │
│ └─────────┘ └────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

Below 768px: sidebar collapses to a hamburger drawer.

## Components

| Component   | Spec                                                          |
|-------------|---------------------------------------------------------------|
| Sidebar     | 260px fixed. Brand mark + name + nav items + "+ New" button   |
| Topbar      | Breadcrumb + page title + actions slot + theme toggle         |
| Card        | `bg-panel`;`r-lg`;`shadow-1`;24px padding                    |
| Button       | Primary (filled accent), secondary (outlined), ghost, icon    |
| Input       | 1px line border;`r-md`;focus ring accent-soft                |
| Tag         | Pill;`r-sm`;10px padding;soft-tinted bg                      |
| Table       | Header in `--subtle` 11px uppercase;row hover `--accent-soft` |
| Modal       | Existing `openConfirmModal`;restyle with new tokens          |
| Empty state | Icon + title + body + action button                           |
| Loading     | Skeleton bars (animated shimmer)                             |
| Toast       | Bottom-right;auto-dismiss 4s (NEW)                           |

## Per-View Layouts

### Watchlist (setup)

- Topbar: "Watchlist" + "Refresh" button
- Hero: "Your watchlist" + count + "Add asset" button
- Table: Symbol · Name · Price · Change · Last analysis · Actions
- "+ Add asset" inline form at top of table (collapsed by default)

### Asset detail

- Topbar breadcrumb: Watchlist / `688836.SS`
- Title: ticker chip + asset name
- Hero card: gradient accent-soft bg · last price · change pill · volume + mkt cap
- Watchlist mini-card: other assets (collapsible)
- Chart card: tabs (1M/3M/6M/1Y) + indicators menu + big chart
- Tabs: Reports · Jobs
  - Reports: title + date · decision tag · "Read" link
  - Jobs: status pill · phase · started · signal · retry button

### New Analysis form

- Topbar: "New analysis"
- Two-column layout:
  - Left: ticker, asset type, analysis date
  - Right: provider, quick model, deep model, output language
- Section: analysts (multi-select cards)
- Section: research depth (radio cards)
- Footer: "Cancel" + "Start analysis" (primary)

### Active jobs

- Topbar: "Active jobs" + cancel button
- Left: phase timeline (Analyst Team → Research → Trading → Risk → Portfolio)
- Right: agent status with pulsing dot + log feed
- Terminal panel at bottom (when failed/cancelled)

### Report detail

- Topbar: breadcrumb + "Download" + "New analysis"
- Two-column:
  - Main: markdown viewer (typography: h1-h6 styled, code blocks, tables)
  - Right: metadata (date, agents, decision, signal, provider)

### Library

- Topbar: "Reports library"
- Toolbar: search · asset filter · status filter · sort
- Table: title · symbol · status · date · signal · actions
- Pagination footer

### Settings

- Topbar: "Settings"
- Sections: Analysis config · Market providers · Theme
- Each section as a card with key-value rows
- Theme toggle: light / dark / system

## Implementation Phases

Each phase ships independently — partial UX improvement after each push.

1. **Foundation** — Replace `styles.css` with new tokens + dark mode. Add
   icon system. Restructure `index.html` with sidebar + topbar skeleton.
2. **Sidebar + Topbar** — Persistent nav. Wire to existing route system.
3. **Watchlist** — Table layout + add-asset inline form.
4. **Asset detail** — Hero card + chart card + tabs.
5. **Analysis form** — Two-column layout + radio cards.
6. **Active jobs** — Timeline + agent status + log feed.
7. **Report detail** — Markdown viewer + metadata sidebar.
8. **Library** — Toolbar + table + pagination.
9. **Settings** — Sectioned cards + theme toggle.
10. **Modals + States** — Empty/loading states + modal restyle.
11. **Responsive + Polish** — Mobile breakpoints + dark mode QA.

Each phase = one commit. Total: ~11 commits.

## Out of Scope

- Charts library swap (keep current pure-SVG implementation)
- Backend API changes (no new endpoints)
- Dark mode per-component theme colors (only token-level swap)
- Animations beyond minimal (hover, focus, transitions)
- Accessibility audit (basic focus rings + semantic HTML only)

## Risks

- 11 commits is a lot — may want to consolidate to 5-6 bigger commits
- Pure-CSS dark mode + monospace numbers may regress visual fidelity on
  dense tables
- Sidebar 260px leaves less horizontal room for charts — verify on 1280px+
- Icon system: inline SVG keeps everything self-contained but adds bytes
- User's terse style — favor execution speed over review ceremony
