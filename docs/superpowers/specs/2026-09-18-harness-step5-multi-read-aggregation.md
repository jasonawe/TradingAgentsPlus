# Harness Step 5 — limit regex expansion, time-range slots, multi-read aggregation

Status: in_progress
Owner: harness
Branch: main
Date: 2026-09-18
Supersedes: nothing
Builds on: 2026-09-17-harness-multi-intent-read-short-circuit.md

## Goal

Three small improvements that round out the Tier 1 read short-circuit:

1. **A — Limit slot regex expansion** — currently `看 600036 笔记 limit 3`
   fails to extract `limit=3`. Tighten the regex to cover the natural
   Chinese variants.
2. **B — Time-range slots** — `近 7 天 / last 24h / 2026-09-01 之后` is
   currently ignored. Extend `ListNotesArgs / ListAlertsArgs /
   ListReportsArgs` with `since_ts / until_ts` (epoch seconds), and
   plumb `time_range` slot through.
3. **C — Multi-read aggregation** — when `_run_multi` produces 2+
   results, the current `agent_final` payload is `{"multi": [...]}` —
   raw, not user-friendly. Render a single concise natural-language
   summary so the UI shows one coherent paragraph rather than a JSON
   blob.

Scope: read-only Tier 1 path. No write changes, no LLM call changes.

## Non-goals

- No Tier 2/3 changes
- No new intents or tools
- No cron / schedule changes
- No LLM call changes for synthesise (still 0 calls in Tier 1)

## Implementation

### A — Limit regex

`tier.py` line 248 currently:
```
re.search(r"(?:最近|前|limit)\s*(\d+)\s*(?:条|个|只|条记录|条笔记)?", lower)
```

Replace with multi-pattern union:
1. `(?P<n>最近|前|前\s*\d+|limit)\s*(\d+)\s*(条|个|只|条记录|条笔记)?` — base
2. `(\d+)\s*(?:条|个|只|条记录|条笔记)` standalone — "5 条笔记"
3. `(?:只|仅|就要)\s*(\d+)\s*(?:条|个|只)?` — "只看 3 条"
4. `(?:top|前)\s*(\d+)` English fallback

Pick the first match, cap at 200.

### B — Time-range slots

`tier.py` already extracts `time_range = (since_iso, until_iso)` at
line ~230 — wire it through.

`builtin.py` schemas:
```python
class ListNotesArgs(BaseModel):
    symbol: Optional[str] = None
    limit: int = 50
    since_ts: Optional[int] = None
    until_ts: Optional[int] = None

# same for ListAlertsArgs, ListReportsArgs
```

`_build_args` in `short_circuit.py` merges `time_range` slot into
`since_ts / until_ts` (convert ISO → epoch).

### C — Multi-read aggregation

`_run_multi` already collects `results`. After the loop, before the
`agent_final` yield, run a small `_summarise_multi(results)` helper:

- Each result carries `intent / tool / result.text / result.count`.
- Output: `"{symbol} 共 {total} 条记录:\n• {intent}: {summary}\n..."`
- Fallback to `"\n".join(r["result"].get("text", "") for r in results)`
  when no count available.

Yield BOTH the raw `multi` payload AND a `summary` field so the UI
can pick whichever it wants to render.

```python
yield ("agent_final", {
    "tier": int(Tier.DIRECT),
    "result": {"multi": results, "count": len(results)},
    "summary": summary_text,
    "rendered": True,
    "multi_intent": True,
})
```

## Acceptance

- `看 600036 笔记 limit 3` → `limit=3`
- `前 10 条笔记` → `limit=10`
- `只看 3 条告警` → `limit=3`
- `近 7 天的笔记` → `since_ts ≈ now - 7d`, `until_ts = now`
- `2026-09-01 之后的笔记` → `since_ts = 2026-09-01 epoch`
- Multi-intent query with 2+ reads → `agent_final.summary` is one
  paragraph, not raw JSON.

## Tests

- tier unit: limit regex variants, time_range extraction
- short_circuit: _build_args merges since_ts/until_ts
- short_circuit: _run_multi emits summary field
- e2e: `/tmp/e2e_step5.py` covers all 6 patterns above

## Risks

- Existing tests assume `ListNotesArgs` has only `symbol/limit`.
  Adding optional `since_ts/until_ts` is additive and backward-compatible.
- Multi-read summary is heuristic — if it's noisy, we can disable by
  `if len(results) < 2: skip summary`.
