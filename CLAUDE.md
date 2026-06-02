# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-purpose script that books a PolyU tennis court 7 days ahead, every
morning at 08:30 HKT. Runs as a GitHub Actions cron job (entrypoint
`book-tennis`, defined in `pyproject.toml`).

## Commands

```bash
uv sync                                   # install deps (Python 3.12+, Playwright)
uv run playwright install chromium        # one-time browser install
uv run pytest                             # run all unit tests (offline, no network/browser)
uv run pytest tests/test_slot_finder.py::test_returns_all_when_all_available
uv run book-tennis --dry-run --skip-sleep # local end-to-end (needs POLYU_USERNAME/POLYU_PASSWORD)
```

Manual workflow trigger (use after watchdog issue, or to test on a branch):

```bash
gh workflow run "Daily Tennis Booking" -f dry_run=false -f skip_sleep=true
gh workflow run "Daily Tennis Booking" --ref <branch> -f ...   # CF Worker only triggers main; use --ref for branch tests
gh run watch <id> --interval 15 --exit-status                  # block until done
```

`--dry-run` walks the full flow but stops before clicking final Submit (so it
doesn't actually book a court). `--skip-sleep` runs immediately instead of
waiting until 08:30 HKT.

Selector discovery (when the PolyU UI changes — see below):

```bash
POLYU_USERNAME=... POLYU_PASSWORD=... \
    uv run python scripts/discover_selectors.py
```

## Architecture

The booking flow spans two files. `src/booker.py` holds per-session
primitives (`login`, `prepare_search`, `submit_search`,
`slot_has_availability`, `click_through`, `submit_and_resolve`).
`src/parallel_runner.py` orchestrates N `PolyUSession` instances in
parallel (N = `len(slot_priority_for(target_date))`, one per priority
slot). Runtime sequence: all sessions do `login` + `prepare_search`
concurrently (before 08:30) → at 08:30:00.000 all fire `submit_search`
simultaneously → each probes its own assigned slot via
`slot_has_availability` → each calls `click_through` (cell-click + Next
+ agreement-tick) → `submit_and_resolve` is gated by a single-dequeuer
coordinator so Submits happen strictly in priority rank order. The first
SUCCESS sets a shared win event and the others exit cleanly.

Things that aren't obvious from a single file:

- **External Cloudflare Worker triggers the workflow.** GitHub Actions'
  scheduled cron proved unreliable (skipped days, multi-hour delays), so a
  Cloudflare Worker (`infra/cloudflare-worker/`) calls `workflow_dispatch`
  API at 07:30 HKT daily. The 60-minute lead absorbs GitHub Actions runner
  queue delays (observed up to 35 min on 2026-05-16, which made the
  previous 08:20 cron miss the 08:30 slot-open). The booker still calls
  `sleep_until_hkt` to land on 08:30:00.000 regardless of when it started. A second cron in the same Worker fires at 08:35 HKT and
  opens a GitHub issue if no successful run exists for the day (the issue
  auto-emails the repo owner). The workflow itself has no `schedule:`
  block — `workflow_dispatch` only.

- **PolyU releases 7-days-ahead slots at EXACTLY 08:30 HKT.** Booking before
  that time sees no available slots; booking late loses popular slots to
  other users. The whole hedged-trigger architecture (CF fires at 07:30,
  runner cold-starts, booker sleeps to 08:30:00.000) exists to land on this
  exact moment with environment pre-warmed. Never propose changes that
  would let the booker run before 08:30 HKT (e.g., `skip_sleep=true`,
  removing the `sleep_until_hkt` call, lowering `TRIGGER_TIME_HKT`).

- **Two-phase sleep — login is intentionally BEFORE 08:30.** `run()` sleeps
  twice: first to `TRIGGER_TIME_HKT - PRELOGIN_LEAD_SECONDS` (08:29:00),
  then runs `login` + `prepare_search` (Tennis dropdown + date), then
  sleeps again to 08:30:00.000, then fires `submit_search`. This lets the
  Search request land within ~1s of the slot-open instant instead of the
  ~20s it took when login started at 08:30. The "never run before 08:30"
  rule applies to the *booking action* (Search, probe, click cell, Submit)
  — logging in earlier is fine and is what makes the 08:30 click hit fast.
  Do not collapse the two sleeps back into one.

- **Parallel booking via single-dequeuer coordinator.**
  `src/parallel_runner.py:run_parallel` runs N `PolyUSession` instances
  concurrently (N = `len(slot_priority_for(target_date))`, one session per
  priority slot). All N do `login` + `prepare_search` before 08:30; at
  08:30:00.000 they all fire Search and probe their assigned slot in
  parallel. The final Submit click is serialized in priority rank order by
  a single-dequeuer coordinator — the first session to successfully Submit
  sets a shared win event, and the others exit cleanly. This replaces the
  old sequential `book_slot` retry loop, which lost popular slots in a
  click-through window long enough for another user to commit during our
  probe→Submit gap.

- **`skip_sleep` default MUST stay `false` in book.yml.** CF Worker calls
  `workflow_dispatch` with no inputs, so defaults apply. If `skip_sleep`
  defaults to `true`, the booker runs at 08:23 (after runner cold-start)
  before PolyU's 08:30 slot-open, and bookings will silently fail. The CF
  Worker also hardcodes `ref: "main"` — testing on a branch requires
  explicit `gh workflow run --ref <branch>`.

- **Exit codes drive notification.** Exit 0 = booked successfully (silent).
  Exit 1 = no slot in any priority window, or any error (login failure,
  selector timeout, etc.) — GitHub emails the workflow owner on failure.
  There is deliberately no success notification path.

- **Selectors are externalized.** `src/config.py:Selectors` holds every CSS
  selector and the Tennis activity dropdown value. They were discovered by
  running `scripts/discover_selectors.py` against the live system. When the
  PolyU UI changes (symptom: `Selector ... is not configured` or a Playwright
  timeout on a specific element), re-run discovery, inspect `artifacts/*.html`,
  and update the dataclass. `require()` raises a clear error if any field is
  left as the `PENDING_DISCOVERY` sentinel.

- **Date format gotcha.** The PolyU page uses two different formats:
  the `searchDate` input expects `DD/MM/YYYY` (slashes), while timeslot cells
  carry `data-slot-date="DD-MM-YYYY"` (dashes). `available_slot_cell` is a
  format-string template; the booker substitutes both forms from the same
  `target_date`. Don't unify them.

- **Datepicker is set via JS, not typed.** `searchDate` is a readonly
  jQuery-UI datepicker. The booker calls `page.evaluate` with
  `$('#searchDate').val(...).trigger('change')` rather than `page.fill`.

- **Login verification.** After submitting credentials, `login()` checks the
  URL still contains `loginhome` or that the username field is still present —
  if so, raises `LoginFailed` distinctly so wrong-credentials show up as a
  meaningful failure, not a downstream selector timeout.

- **Race window is probe→Submit; failures advance to next rank.**
  PolyU only commits the slot on final Submit, so another user can grab
  it any time during probe → click → Next → checkbox → Submit (~3s
  window). Each session targets exactly one assigned slot: if
  `slot_has_availability` returns false, the session raises
  `_SlotUnavailable` and exits cleanly — the coordinator skips it and
  advances to the next rank. If `submit_and_resolve` returns
  `BookingResult.OCCUPIED` ("Facility is occupied" banner) or `ERROR`,
  the coordinator moves on to the next ready session. Screenshots are
  per-session (e.g. `failure_s0.png`, `failure_s1.png`).

- **Submit detects failure in ~1s, not 20s.** After clicking Submit,
  `submit_and_resolve` races two waiters: `wait_for_url` (success — page
  navigates away from `make_book_submit.do`) against `wait_for_selector`
  on the "Facility is occupied" banner (known failure). Without the race,
  the URL waiter eats the full 20s `DEFAULT_TIMEOUT_MS` before the
  coordinator can advance to the next rank — by which time the
  next-priority slot is also gone. The known banner phrase is hardcoded;
  unknown error pages still fall through to the 20s timeout and are
  captured in `post_submit_<session_id>.png`.

- **Password redaction.** `src/log.py:build_logger` installs a filter that
  replaces the password string with `***` in log messages and args before any
  handler sees them. Playwright errors can quote field values, so all logging
  must go through this logger — don't `print()` or use the root logger.

- **Artifacts.** `click_through` saves `pre_submit_<session_id>.png`
  (after ticking the agreement checkbox, before final Submit).
  `submit_and_resolve` saves `post_submit_<session_id>.png` (after Submit
  or on known failure). CI uploads the `artifacts/` directory on every
  run, including failures. `pre_submit_s0.png` (the rank-0 session) is
  the key artifact to inspect after a dry-run smoke test.

- **Dry-run smoke tests are time-dependent.** `--dry-run --skip-sleep`
  exercises the full flow up to (but not including) Submit, but only if a
  priority slot is actually free. After 08:30 HKT, popular slots are gone;
  each session's `slot_has_availability` probe returns false, the session
  raises `_SlotUnavailable`, and the run exits with code 1 before any
  `pre_submit_*.png` is produced. To validate `click_through` and
  `submit_and_resolve` end-to-end, dry-run before 08:30 HKT or temporarily
  widen `SLOT_PRIORITY` to include a known-free off-peak window.

- **Tests are offline.** All tests in `tests/` use fakes (e.g. `make_probe`
  in `test_slot_finder.py`) — no network, no Playwright launch. Don't add
  live integration tests; verify changes by running `--dry-run` against the
  real site locally and checking artifacts.

- **CI sets `TZ: Asia/Hong_Kong`.** The workflow exports this so the runner's
  wall clock matches HKT, which is what `dates.py:now_hkt()` and the cron
  comments assume. Don't remove it from `.github/workflows/book.yml`.

## Tuning knobs

- Slot preferences: `SLOT_PRIORITY` in `src/config.py` (tuple of `(start, end)`, tried in order).
- Trigger time: `TRIGGER_TIME_HKT` in `src/config.py`.
- Days-ahead window: `DAYS_AHEAD` in `src/dates.py`.

## Weekday-specific exclusions

`config.slot_priority_for(target_date)` filters `SLOT_PRIORITY` per weekday
before sessions are created. When it returns an empty tuple, `run()`
short-circuits with exit 0 (no sleep, no Playwright launch) — the watchdog
treats the day as accounted for and does not open an issue. Currently:

- **Tuesday is a rest day.** `target_date.weekday() == 1` is in
  `_REST_WEEKDAYS`, so Tuesday-target runs skip booking entirely. (The
  18:30-20:30 cells are staff-reserved anyway, and the remaining slots
  aren't wanted.)

Add new rest weekdays to `_REST_WEEKDAYS`. For partial exclusions (some
slots skipped but the day still booked), reintroduce a frozenset of
`(start, end)` tuples and filter `SLOT_PRIORITY` against it in
`slot_priority_for`.

## Design docs

Background and rationale live under `docs/superpowers/specs/` and
`docs/superpowers/plans/` — read the most recent files there before
substantive changes.
