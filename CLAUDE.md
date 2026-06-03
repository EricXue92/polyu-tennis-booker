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

The booking flow spans three files. `src/booker.py:run` does Playwright
login at 08:29, extracts session state (cookies + CSRFToken + fbUserId)
via `bootstrap_http_client`, closes the browser, sleeps to 08:30:00.000,
and hands off to `src/http_booker.py:book_via_http`. That orchestrator
calls `PolyUHttpClient.search()` (one HTTP POST), then iterates priority
slots in rank order calling `client.try_book()` serially: SUCCESS wins,
OCCUPIED advances to the next rank, ERROR aborts. The client
(`src/http_client.py`) issues all three booking POSTs (timetable.json,
make_book.do, make_book_submit.do) over raw httpx — no Playwright on
the hot path.

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

- **Two-phase sleep — login is intentionally BEFORE 08:30.** `run()`
  sleeps twice: first to `TRIGGER_TIME_HKT - PRELOGIN_LEAD_SECONDS`
  (08:29:00), runs Playwright `login` (~2-3s) and `bootstrap_http_client`
  to extract cookies + CSRFToken + fbUserId, closes the browser, then
  sleeps again to 08:30:00.000 before calling `book_via_http`. HTTP
  login is much faster than the old Playwright login + dropdown + date
  flow, but we keep the 60s lead as a buffer — running closer than that
  risks landing the Search POST a few hundred ms after 08:30 if the CI
  runner is busy. Do not collapse the two sleeps into one — landing
  Search exactly at 08:30:00.000 is the entire point.

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

- **HTTP request shapes are externalized too.** `scripts/capture_http.py`
  is the HTTP analogue of `discover_selectors.py`: it runs the booking
  flow under Playwright with `context.on("request"/"response")` hooks
  and dumps `artifacts/http_trace.json` (passwords and cookie values
  redacted). Used by `src/http_client.py` to build/maintain the raw
  HTTP request templates that replace Playwright on the hot path.
  Re-run when PolyU changes its booking endpoints or form fields
  (symptom: `PolyUHttpClient` 4xxs or returns unexpected response
  shape). Requires a known free off-peak slot — see the comment block
  at the top of the script for usage.

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

- **Race window is search→Submit; failures advance to next rank.**
  `book_via_http` POSTs the Search at 08:30:00, parses the JSON response
  to find which (facility, time) pairs are free, then POSTs cell-click +
  final Submit serially in priority rank order. Race window from Search
  fire to first Submit hitting PolyU: ~4.5-5.0s (dominated by PolyU's
  ~4s server-side Search latency, then 2 cheap httpx POSTs). If the
  first try_book returns OCCUPIED, the orchestrator advances to the
  next rank within ~400ms. ERROR (auth lost, 5xx, unexpected redirect)
  aborts the whole run to avoid burning through priorities on a broken
  session — the watchdog email then has a meaningful failure to surface.

- **Submit success/failure detection is direct, no timeout.** With
  Playwright we raced `wait_for_url` against `wait_for_selector("Facility
  is occupied")` to avoid a 20s URL-waiter eating the rank-advance
  budget. With HTTP, `try_book` inspects the Submit response's Location
  header: `make_book_result.do` ⇒ SUCCESS, banner in body or
  `make_book_submit.do` ⇒ OCCUPIED, anything else ⇒ ERROR. Result is
  decided in one round-trip (~400ms) instead of a 20s waiter race.

- **Password redaction.** `src/log.py:build_logger` installs a filter that
  replaces the password string with `***` in log messages and args before any
  handler sees them. Playwright errors can quote field values, so all logging
  must go through this logger — don't `print()` or use the root logger.

- **Artifacts.** The HTTP flow doesn't produce screenshots — the
  request/response shapes are baked into `src/http_client.py` and tested
  offline. For new live failures (e.g. PolyU UI changes), use
  `scripts/capture_http.py` to grab a fresh HTTP trace under Playwright
  (login + make_book.do navigation only, since the full booking flow no
  longer needs Playwright). CI uploads any `artifacts/` content if
  present, but the booker itself doesn't create files anymore.

- **Dry-run smoke tests are time-dependent.** `--dry-run --skip-sleep`
  exercises the full HTTP flow (search + try_book serially in rank order),
  but only if a priority slot is actually free. After 08:30 HKT, popular
  slots are gone and `book_via_http` exits with code 1 before any booking
  is placed. To test the full flow end-to-end, dry-run before 08:30 HKT
  or temporarily widen `SLOT_PRIORITY` to include a known-free off-peak
  window.

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
