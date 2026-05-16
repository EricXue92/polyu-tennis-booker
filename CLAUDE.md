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

The booking flow is intentionally linear and lives almost entirely in
`src/booker.py`. Reading it top-to-bottom matches the runtime sequence:
sleep until pre-login → `login` → `prepare_search` (Tennis dropdown +
date) → sleep until 08:30:00.000 → `submit_search` (Search click) →
`pick_slot` (probes all slots concurrently via `asyncio.gather`, returns
available slots in `SLOT_PRIORITY` order) → `book_slot` (with retry via
`restart_to_results` on failure — see race-window bullet).

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

- **Race window is probe→Submit; failures fall back to next priority.**
  PolyU only commits the slot on final Submit, so another user can grab
  it any time during probe → click → Next → checkbox → Submit (~4s
  window). On `BookingFailed` (incl. "Facility is occupied" banner) or
  Playwright timeout, `run()` calls `restart_to_results` (goto LOGIN_URL
  → re-prep → re-Search), re-probes the next candidate from `pick_slot`'s
  list, and retries. Screenshots are overwritten per attempt.

- **Password redaction.** `src/log.py:build_logger` installs a filter that
  replaces the password string with `***` in log messages and args before any
  handler sees them. Playwright errors can quote field values, so all logging
  must go through this logger — don't `print()` or use the root logger.

- **Artifacts.** `book_slot` saves `pre_submit.png` (after ticking the
  agreement checkbox, before final Submit), `post_submit.png` (after Submit),
  and `failure.png` on any exception. CI uploads the `artifacts/` directory
  on every run, including failures. `pre_submit.png` is the key thing to
  inspect after a dry-run smoke test. `search_results.png` is captured at
  the default viewport (~640px) which only covers the morning slot rows —
  it cannot confirm whether evening `SLOT_PRIORITY` slots were free or
  taken. Use the log line "no slot available" as the authoritative signal,
  not the screenshot.

- **Dry-run smoke tests are time-dependent.** `--dry-run --skip-sleep`
  exercises the full flow up to (but not including) Submit, but only if a
  priority slot is actually free. After 08:30 HKT, popular slots are gone,
  `pick_slot` returns an empty list, the flow exits with code 1 BEFORE
  `book_slot` runs, and no `pre_submit.png` is produced. To validate `book_slot`
  end-to-end, dry-run before 08:30 HKT or temporarily widen `SLOT_PRIORITY`
  to include a known-free off-peak window.

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

## Design docs

Background and rationale live under `docs/superpowers/specs/` and
`docs/superpowers/plans/` — read the most recent files there before
substantive changes.
