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
uv run pytest tests/test_slot_finder.py::test_picks_first_priority_when_all_available
uv run book-tennis --dry-run --skip-sleep # local end-to-end (needs POLYU_USERNAME/POLYU_PASSWORD)
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
`login` → `navigate_to_search` → `pick_slot` (probes via `slot_has_availability`
in priority order from `SLOT_PRIORITY`) → `book_slot`.

Things that aren't obvious from a single file:

- **Three hedged cron starts.** `.github/workflows/book.yml` fires at 08:15,
  08:20, and 08:25 HKT. Each job sleeps in-process via `sleep_until_hkt` until
  exactly 08:30:00.000 before issuing the booking. GitHub's cron can be
  delayed 5–30 min, so multiple starts protect against a single late fire.
  All three may end up running; only the first to grab a slot succeeds — the
  others exit 1 with "no slot available", which is fine.

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

- **Race on probe→click.** `pick_slot` probes availability and `book_slot`
  re-locates the cell to click it. There's no DOM snapshot in between — if
  someone else books in the gap, `.click()` times out and we treat it as a
  normal booking failure. This is by design (per the design spec).

- **Password redaction.** `src/log.py:build_logger` installs a filter that
  replaces the password string with `***` in log messages and args before any
  handler sees them. Playwright errors can quote field values, so all logging
  must go through this logger — don't `print()` or use the root logger.

- **Artifacts.** `book_slot` saves `pre_submit.png` (after ticking the
  agreement checkbox, before final Submit), `post_submit.png` (after Submit),
  and `failure.png` on any exception. CI uploads the `artifacts/` directory
  on every run, including failures. `pre_submit.png` is the key thing to
  inspect after a dry-run smoke test.

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
