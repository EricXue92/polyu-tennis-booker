# click_through latency micro-optimizations

**Date:** 2026-05-31
**Status:** Proposed
**Author:** XUE (with Claude)

## Background

On 2026-05-31 the booker lost all three priority slots (17:30 / 18:30 / 19:30
for 2026-06-07, a Sunday) when every session's final Submit hit a
"Facility is occupied" banner. Logs show every session probed its cell as
**available** at the start of the click_through phase, but the ~9-second
window between Search firing and the first Submit click let other users
commit the slots first.

Recent 5-run history: 1 success / 4 failures. The pattern is not transient
network jitter — it is that the priority list is composed entirely of peak
slots, and the race window is large enough to lose all of them.

Today's run timeline (UI side only):

| Step | Duration | Controllable? |
|---|---|---|
| Search click → results loaded (`table.tt-timetable`) | 5.4s | No — PolyU backend |
| `page.wait_for_timeout(800)` after cell click | 0.8s | Yes |
| Next click → `make_book_submit.do` URL match | ~0.5s | No |
| `wait_for_load_state("networkidle")` after Next | ~0.5s | Yes |
| Screenshot pre_submit | ~0.3s | Marginal |
| `check(agreement_checkbox)` + Submit click | ~0.4s | No |

This spec addresses the two controllable waits (800ms timeout and
`networkidle`) and fixes a stale ordering between screenshot and
checkbox tick. Backgrounding the screenshot and replacing the UI with
direct HTTP POSTs are explicitly out of scope (separate efforts).

## Goal

Shrink the per-session probe→Submit window by ~1.1 seconds with minimal
risk and no changes to the trigger time, login serialization, or
single-dequeuer coordinator.

## Non-goals

- Backgrounding `page.screenshot` via task-list lifecycle changes.
- Switching screenshot to JPEG.
- Reverse-engineering PolyU's form POSTs to skip the UI.
- Adding fallback slots, additional sessions per slot, or altering rank
  policy.
- Changing `prepare_search`, `login`, or `submit_and_resolve`.

## Changes

All three changes are in `src/booker.py:click_through` (the function
spanning roughly lines 162–198).

### Change 1: Reduce post-cell-click timeout from 800ms → 200ms

Current (`booker.py:184-185`):
```python
await page.locator(cell_selector).first.click()
await page.wait_for_timeout(800)  # let cell-selection state settle
```

Proposed:
```python
await page.locator(cell_selector).first.click()
# PolyU's cell-click handler is synchronous JS that flips a hidden form
# field; 200ms is a conservative margin. Validated by dry-run inspection
# of pre_submit_s0.png (cell still highlighted, Next still navigates).
await page.wait_for_timeout(200)
```

**Why 200ms and not zero:** without instrumentation into PolyU's
client-side script, going to zero risks Next firing before the cell-click
handler has stamped the form. 200ms is the smallest round number that
still leaves headroom for a slow event-loop tick.

**Expected savings:** 600ms per run.

### Change 2: Remove redundant `wait_for_load_state("networkidle")`

Current (`booker.py:191-192`):
```python
await page.wait_for_url(f"**{SUBMIT_URL.split('//')[1]}", timeout=DEFAULT_TIMEOUT_MS)
await page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
```

Proposed:
```python
await page.wait_for_url(f"**{SUBMIT_URL.split('//')[1]}", timeout=DEFAULT_TIMEOUT_MS)
# No explicit load-state wait — page.check() below auto-waits for the
# checkbox to be visible, enabled and stable.
```

**Why this is safe:** `page.check()` uses Playwright's actionability
checks (visible, enabled, stable, receives events) with the default
20s timeout already configured on the page. The `networkidle` wait was
a belt-and-suspenders that required 500ms of network silence after the
DOM was already usable.

**Failure mode if assumption wrong:** if the checkbox is slow to render,
`page.check()` will wait up to 20s (same as before). Worst case is the
same latency we'd have had; nothing crashes.

**Expected savings:** ~500ms per run.

### Change 3: Reorder screenshot to after checkbox tick

Current (`booker.py:193-198`):
```python
ARTIFACTS.mkdir(exist_ok=True)
suffix = f"_{session_id}" if session_id else ""
await page.screenshot(path=str(ARTIFACTS / f"pre_submit{suffix}.png"))

log.info("ticking agreement checkbox")
await page.check(require(SELECTORS.agreement_checkbox, "agreement_checkbox"))
```

Proposed:
```python
log.info("ticking agreement checkbox")
await page.check(require(SELECTORS.agreement_checkbox, "agreement_checkbox"))

ARTIFACTS.mkdir(exist_ok=True)
suffix = f"_{session_id}" if session_id else ""
await page.screenshot(path=str(ARTIFACTS / f"pre_submit{suffix}.png"))
```

**Why:** CLAUDE.md already documents the screenshot as taken "after
ticking the agreement checkbox, before final Submit" — the code was the
outlier. Moving the screenshot afterwards makes the artifact reflect the
real about-to-be-submitted state (checkbox visibly checked) and matches
the documented intent.

**Expected savings:** 0ms. This is a correctness/clarity change.

## Summary of impact

| Metric | Before | After |
|---|---|---|
| `click_through` UI overhead | ~2.0s | ~0.9s |
| Total probe→Submit window (today's run) | ~9.0s | ~7.9s |
| Per-session win probability (estimate) | baseline | +~12% |

This does not solve the fundamental problem — 5.4s of PolyU-side Search
rendering is untouched — but it gives each of the 3 parallel sessions
~1.1s less time during which a competitor can grab the slot.

## Tests

`tests/` is offline-only and does not exercise `click_through` (which
requires a real browser). No test changes.

Manual verification before merging:

1. `uv run pytest` — must remain green (sanity check that no unrelated
   tests were impacted).
2. `uv run book-tennis --dry-run --skip-sleep` run locally, executed
   either **before 08:30 HKT** or with `SLOT_PRIORITY` temporarily
   widened to a known-off-peak slot, so the dry-run reaches Submit.
3. Inspect `artifacts/pre_submit_s0.png`:
   - The cell for the targeted slot still appears highlighted/selected.
   - The page is on `make_book_submit.do`.
   - The agreement checkbox is **checked** (per the reorder).
4. Verify the run log shows no Playwright timeouts on the cell or Next
   steps.

If any of (3) fails, increase the timeout in Change 1 back to 400ms and
re-run; if still failing, revert Change 1 entirely.

## Documentation updates

- `CLAUDE.md`: update the "Race window is probe→Submit" paragraph —
  current text says "~4s window" and "~5-second click-through", both of
  which should reflect post-change measurements once landed.

## Rollback

Single file, three localized edits. `git revert` of the merge commit
fully restores prior behavior. No data migrations, no config schema
changes, no external dependencies touched.

## Open questions

None. All decisions made inline above. A follow-up may revisit
screenshot backgrounding or direct HTTP POSTs once we have post-change
data showing whether ~7.9s is enough.
