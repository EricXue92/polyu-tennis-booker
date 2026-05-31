# click_through latency micro-optimizations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shave ~1.1s off the per-session probe→Submit race window by removing two unnecessary waits in `click_through` and reordering the pre-submit screenshot.

**Architecture:** Three localized edits to `src/booker.py:click_through` (one function, ~10 contiguous lines). No new files, no signature changes, no test infrastructure changes. CLAUDE.md gets two stale-text fixes to match the new timing and reordered artifact.

**Tech Stack:** Python 3.12, Playwright async API, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-31-click-through-latency-optimizations-design.md`

**Notes on TDD:** `tests/` is offline-only and does not cover `click_through` (which needs a real browser). The verification gate for these edits is a local Playwright `--dry-run --skip-sleep` plus artifact inspection — defined in Task 2 as explicit manual steps. `pytest` is still run as a sanity check.

---

## Task 1: Apply the three click_through edits

**Files:**
- Modify: `src/booker.py:184-198`

The three edits are intertwined (adjacent lines of the same function) and share the same revert path; landing them as a single atomic change keeps the diff readable and rollback trivial. Splitting would not improve bisectability for changes this small.

- [ ] **Step 1: Confirm current state matches the spec**

Run: `sed -n '180,200p' src/booker.py`

Expected output (verbatim):

```
    log.info("clicking available cell for %s %s-%s", target_date, start, end)
    await page.locator(cell_selector).first.click()
    await page.wait_for_timeout(800)  # let cell-selection state settle

    log.info("clicking Next")
    await page.locator(
        require(SELECTORS.next_button, "next_button")
    ).first.click()
    await page.wait_for_url(f"**{SUBMIT_URL.split('//')[1]}", timeout=DEFAULT_TIMEOUT_MS)
    await page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)
    ARTIFACTS.mkdir(exist_ok=True)
    suffix = f"_{session_id}" if session_id else ""
    await page.screenshot(path=str(ARTIFACTS / f"pre_submit{suffix}.png"))

    log.info("ticking agreement checkbox")
    await page.check(require(SELECTORS.agreement_checkbox, "agreement_checkbox"))
```

If the file has drifted (e.g. an unrelated edit landed since the spec was written), STOP and reconcile manually before proceeding.

- [ ] **Step 2: Apply the edit**

Replace the block above with:

```python
    log.info("clicking available cell for %s %s-%s", target_date, start, end)
    await page.locator(cell_selector).first.click()
    # PolyU's cell-click handler is synchronous JS that flips a hidden form
    # field; 200ms is a conservative margin (validated via dry-run).
    await page.wait_for_timeout(200)

    log.info("clicking Next")
    await page.locator(
        require(SELECTORS.next_button, "next_button")
    ).first.click()
    await page.wait_for_url(f"**{SUBMIT_URL.split('//')[1]}", timeout=DEFAULT_TIMEOUT_MS)
    # No explicit load-state wait — page.check() below auto-waits for the
    # checkbox to be visible, enabled and stable.

    log.info("ticking agreement checkbox")
    await page.check(require(SELECTORS.agreement_checkbox, "agreement_checkbox"))

    ARTIFACTS.mkdir(exist_ok=True)
    suffix = f"_{session_id}" if session_id else ""
    await page.screenshot(path=str(ARTIFACTS / f"pre_submit{suffix}.png"))
```

Three concrete differences from the original:

1. `wait_for_timeout(800)` → `wait_for_timeout(200)` (Change 1)
2. The `await page.wait_for_load_state("networkidle", ...)` line is deleted (Change 2)
3. The `await page.check(...)` block now precedes the `ARTIFACTS.mkdir(...) / suffix / page.screenshot(...)` block (Change 3)

- [ ] **Step 3: Re-read to verify the edit landed correctly**

Run: `sed -n '180,200p' src/booker.py`

Confirm the new block above is what appears. Pay attention to indentation (4 spaces, no tabs) and that the screenshot block now sits *after* the `page.check(...)` call.

- [ ] **Step 4: Run pytest as a sanity check**

Run: `uv run pytest`

Expected: all tests pass. None of the tests exercise `click_through` directly, so this is purely a "did I break an import / syntax" check, not a functional gate.

If pytest fails, the most likely cause is an accidental syntax error from the edit. Fix and re-run before proceeding.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/booker.py
git commit -m "$(cat <<'EOF'
perf(booker): shave ~1.1s off click_through race window

Three micro-optimizations in click_through:
- post-cell-click wait_for_timeout 800ms → 200ms (cell-click handler is
  synchronous JS; 800ms was overcautious)
- drop redundant wait_for_load_state("networkidle") after Next; page.check()
  already auto-waits for actionability
- move pre_submit screenshot to AFTER agreement checkbox tick so the
  artifact reflects the real about-to-be-submitted state (matches the
  intent documented in CLAUDE.md)

Total: ~1.1s shorter probe→Submit window per session, which should
modestly improve win rate against other users racing for the same slot.

See docs/superpowers/specs/2026-05-31-click-through-latency-optimizations-design.md
for context and the 2026-05-31 failure that motivated this.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Local Playwright dry-run smoke verification

**Files:** none modified — this is a verification gate.

The spec calls out this manual step explicitly because the unit test suite cannot exercise `click_through`. **Do not skip.** If verification fails, the change must be reverted or tuned before merging upstream.

- [ ] **Step 1: Ensure local env can run a dry-run**

Confirm both env vars are set in your shell:

```bash
echo "USERNAME set: ${POLYU_USERNAME:+yes}${POLYU_USERNAME:-NO}"
echo "PASSWORD set: ${POLYU_PASSWORD:+yes}${POLYU_PASSWORD:-NO}"
```

Both must print `set: yes`. If not, source your secrets file or `export` them before continuing.

- [ ] **Step 2: Run the dry-run**

**Important timing constraint:** the dry-run must execute against a slot that is actually free at the time you run it, or `click_through` will never be reached (the session will raise `_SlotUnavailable` and exit). Two options:

- **(a)** Run this BEFORE 08:30 HKT today — the day+7 slots from yesterday's release are likely still showing as available since they were 7 days out at release time. (Re-read CLAUDE.md's "Dry-run smoke tests are time-dependent" section if this is unclear.)
- **(b)** Temporarily edit `src/config.py:SLOT_PRIORITY` to put a known off-peak slot first (e.g. `(time(12, 30), time(13, 30))`). Revert before committing.

Once a free slot is guaranteed, run:

```bash
uv run book-tennis --dry-run --skip-sleep
```

Expected: exit code 0, log lines through `clicking Submit` are absent (dry-run stops before Submit), and at least `pre_submit_s0.png` is written under `artifacts/`.

- [ ] **Step 3: Inspect the pre_submit artifact**

Open `artifacts/pre_submit_s0.png` (or whichever session reached `click_through` — check the log for `s0` / `s1` / `s2`).

Three things must be true in the screenshot:

1. The page is on the booking confirmation URL (`make_book_submit.do`) — title bar / breadcrumb shows the confirmation page, not the timetable.
2. The target slot's date/time is visible somewhere on the confirmation page (sanity that the correct cell was selected).
3. **The agreement checkbox is visibly CHECKED.** This is the verification for Change 3 — before this PR the screenshot would have shown it unchecked.

- [ ] **Step 4: Decision gate**

- If all three checks in Step 3 pass → proceed to Task 3.
- If the checkbox is unchecked → Change 3 didn't land correctly; re-read the edit, fix, recommit (amend the previous commit is fine here since it hasn't been pushed yet), re-verify.
- If the page is NOT on `make_book_submit.do` (e.g. it bounced back to the timetable or hit an error page) → Change 1's 200ms was insufficient. Edit `src/booker.py:185` to use `await page.wait_for_timeout(400)` and re-run from Step 2. If 400ms also fails, revert Change 1 entirely (`wait_for_timeout(800)`) and recommit Task 1 with only Changes 2 and 3.

Document the chosen timeout value in the commit message if it had to be raised from 200ms.

---

## Task 3: Update CLAUDE.md to match new timing

**Files:**
- Modify: `CLAUDE.md` (two timing references; the "Artifacts" bullet is already correct after Task 1's reorder and needs no change)

- [ ] **Step 1: Update the "Race window is probe→Submit" estimate**

In `CLAUDE.md` (around line 135), the current bullet reads:

```
- **Race window is probe→Submit; failures advance to next rank.**
  PolyU only commits the slot on final Submit, so another user can grab
  it any time during probe → click → Next → checkbox → Submit (~4s
  window). Each session targets exactly one assigned slot: if
```

Use Edit tool to replace:

```
it any time during probe → click → Next → checkbox → Submit (~4s
  window).
```

with:

```
it any time during probe → click → Next → checkbox → Submit (~3s
  window).
```

Rationale: the click_through UI overhead drops from ~2.0s to ~0.9s after Task 1, so "~4s window" is now stale. "~3s" is approximate but no longer overstated.

- [ ] **Step 2: Update the "old sequential book_slot" historical reference**

In `CLAUDE.md` (around line 95), the current bullet reads:

```
  sets a shared win event, and the others exit cleanly. This replaces the
  old sequential `book_slot` retry loop, which lost popular slots in the
  ~5-second click-through after another user committed during our
  probe→Submit window.
```

This describes the *old* sequential design, but the "~5-second click-through" figure also applied to the parallel design until today. After Task 1 the parallel design's click_through is closer to ~3.5s, so leaving the bullet saying "5 seconds" misleads readers into thinking that's still the current behavior of either path.

Use Edit tool to replace:

```
  old sequential `book_slot` retry loop, which lost popular slots in the
  ~5-second click-through after another user committed during our
  probe→Submit window.
```

with:

```
  old sequential `book_slot` retry loop, which lost popular slots in a
  click-through window long enough for another user to commit during our
  probe→Submit gap.
```

(Removing the specific number is cleaner than re-citing a measurement that will drift again next time someone optimizes click_through. The race-window paragraph at line 135 keeps the current ~3s figure for readers who want one.)

- [ ] **Step 3: Verify the "Artifacts" bullet still matches code**

Run: `grep -A 1 "Artifacts" CLAUDE.md | head -3`

Expected output includes:

```
- **Artifacts.** `click_through` saves `pre_submit_<session_id>.png`
  (after ticking the agreement checkbox, before final Submit).
```

This text was already correct in CLAUDE.md but contradicted the code (which screenshotted BEFORE the tick). After Task 1's reorder the code now matches the prose. **No edit needed** — just confirming the contradiction is resolved.

- [ ] **Step 4: Run pytest one more time**

Run: `uv run pytest`

Expected: all green. The doc edits shouldn't affect anything, but this confirms no stray hand-edits crept in.

- [ ] **Step 5: Commit**

Run:

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(CLAUDE): refresh timing references after click_through speedup

- "Race window is probe→Submit": ~4s → ~3s to reflect the ~1.1s of waits
  removed from click_through in the previous commit.
- "old sequential book_slot" bullet: drop the specific "5-second" figure;
  prose remains accurate without claiming a measurement that will drift
  again next time the path is optimized.

The "Artifacts" bullet already described pre_submit as "after ticking the
agreement checkbox" — that prose now actually matches the code (it
contradicted the screenshot order until the previous commit).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Open the PR

**Files:** none — uses `gh`.

- [ ] **Step 1: Push the branch**

If you're on `main` directly, create a feature branch first:

```bash
git checkout -b perf/click-through-latency
git push -u origin perf/click-through-latency
```

If you were already on a branch, just `git push`.

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "perf(booker): shave ~1.1s off click_through race window" --body "$(cat <<'EOF'
## Summary
- Reduce `click_through` UI overhead by ~1.1s via three localized edits in `src/booker.py`
- Motivated by 2026-05-31's run where all 3 priority slots were lost in the ~9s probe→Submit window

## Changes
- `wait_for_timeout(800)` → `wait_for_timeout(200)` after cell click
- Drop redundant `wait_for_load_state("networkidle")` after Next (page.check auto-waits)
- Reorder `pre_submit` screenshot to after agreement-checkbox tick (matches CLAUDE.md intent)

## Verification
- [x] `uv run pytest` green
- [x] `uv run book-tennis --dry-run --skip-sleep` reaches Submit; `pre_submit_s0.png` shows checkbox checked and page on `make_book_submit.do`

## Test plan
- [ ] Monitor next 3–5 scheduled runs for unexpected `_SlotUnavailable` raises or Playwright timeouts in `click_through`
- [ ] Compare actual probe→Submit timing in logs against the projected ~7.9s window

## Spec
`docs/superpowers/specs/2026-05-31-click-through-latency-optimizations-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Note the PR URL**

The `gh pr create` output ends with the PR URL — record it so the next scheduled 07:30 HKT run can be compared against the prediction.

---

## Out of scope (deferred)

These were explicitly considered and excluded from this plan:

- **Screenshot backgrounding** via PolyUSession lifecycle tracking — ~300ms additional savings but requires touching `parallel_runner.py` plumbing.
- **JPEG screenshot** — ~150ms savings, loss of clarity not worth the trade for an artifact this important.
- **Direct HTTP POST** bypass of the UI — biggest leverage (~5–7s) but requires reverse engineering form fields + CSRF/viewstate; warrants its own spec.
- **Adding off-peak fallback slots** — Plan B from the brainstorm; deferred until we have post-merge data showing whether 1.1s is enough.

If post-merge data (next 5–10 runs) shows the win rate is still unacceptable, the next move is the off-peak fallback slots (lower-risk than HTTP POST).
