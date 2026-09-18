# Harness Step 6+8 — Intent disambiguation + Result display view

Status: in_progress
Owner: harness
Branch: main
Date: 2026-09-18
Supersedes: nothing
Builds on: 2026-09-18-harness-step5-multi-read-aggregation.md

## Problem

Two compounding issues make several common queries route wrong:

1. **body_md over-capture** — Step 3's slot-aware override fires on any
   `笔记: <text>` style match. But the same regex also matches "笔记
   limit 3" / "note-only 3" — interpreting the limit as the note body
   and routing to (NOTE, CREATE) when the user wanted (NOTE) list.

2. **trade_date over-capture** — Step 3's trade_date regex matches any
   `YYYY-MM-DD` token. But "2026-09-01 之后 600036 的笔记" has an ISO
   date as a *time filter*, not as the analysis target date. Today this
   routes to (RUN, CREATE) + `trade_date=2026-09-01` instead of the
   intended NOTE/LIST with `since_ts=2026-09-01`.

Compounding: when entity detection sees the word 笔记 the same token
is treated both as a noun ("the notes") and as a CREATE trigger when
followed by body content. The classifier needs to disambiguate.

## Goal

### Step 6 — Intent disambiguation

Three rules:

A. **body_md override** — disable body_md capture when:
   - the captured body matches `^\s*(limit\s*)?\d+\s*(条|个|只|条记录|条笔记)?\s*$` (a pure limit phrase)
   - the captured body matches `^\s*(前|最近)\s*\d+\s*(条|个|只)?\s*$`
   - the captured body is empty or whitespace
   The body_md slot still fires for genuine writes like "加一句 X" /
   "记一下 X" / "备注: X".

B. **trade_date override** — gate on a keyword before extracting ISO:
   - require `today / 今天 / 今日 / tomorrow / 明天 / 明日 / 交易日 / 分析日`
     in the message OR no time_range slot present (i.e. user said
     "用 2026-09-01" not "2026-09-01 之后").
   - when an ISO date is present AND `time_range` was already extracted
     from the same message, skip trade_date (Step 5.B's since_ts wins).

C. **NOTE/LIST promotion** — when entity=NOTE/ALERT/REPORT/WATCHLIST
   and slots include `limit` (and not `body_md`), force Op=LIST even if
   some ambiguous verb ("加") appears later in the message. Limit
   itself implies read.

### Step 8 — Tool result display view

Add `display_fields` and `summary_view` to Result schemas, and use
them in `short_circuit._safe_dump` and the agent_final payload so the
UI never sees raw tool internals.

A. **`Result.display_view()` method** — every Result BaseModel class
   (ListNotesResult, ListAlertsResult, ListReportsResult,
   ListRunsResult, ListAlertsResult, ListNotesResult,
   ListScheduledTasksResult) gains a method:

   ```python
   class ListNotesResult(BaseModel):
       text: str
       count: int = 0
       items: list[dict] = []

       def display_view(self) -> dict:
           return {
               "summary": f"{self.count} 条笔记",
               "preview": self.text[:200],
               "count": self.count,
           }
   ```

B. **`short_circuit._safe_dump` calls display_view** — when the result
   has the method. Falls back to model_dump for plain dicts.

D. **PlanTemplateResult** — when rendering through the template engine,
   use display_view() output as the template context, so templates see
   `summary` / `preview` / `count` rather than raw `text` / `count` /
   `items`.

## Acceptance

- `'600036 笔记 limit 3'` → (NOTE, LIST) with limit=3 ✅
- `'2026-09-01 之后 600036 的笔记'` → (NOTE, LIST) with since_ts=2026-09-01 ✅
- `'记一下 600036 估值偏低'` → (NOTE, CREATE) with body_md='估值偏低' ✅
- Tier 1 path for `'600036 笔记 limit 3'` hits list_notes (no
  create_note / no HITL gate)
- agent_final.result for read tools goes through display_view(), not
  raw model_dump()

## Tests

- tier unit: body_md override gates
- tier unit: trade_date override gates
- classify unit: NOTE/LIST promotion on limit-only slot
- short_circuit: _safe_dump calls display_view when available
- e2e: `/tmp/e2e_step6_8.py`

## Risks

- display_view() assumes Result schemas stay small (text + count).
  Adding many items to Result would require updating display_view()
  for that class.
- Step 8 is opt-in via the method's existence — existing Result
  classes keep working via fallback to model_dump.
