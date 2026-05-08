# PolyU Tennis Booking Automation — Design

**Date**: 2026-05-09
**Status**: Approved (pending implementation-phase verification of UI selectors)

## Goal

Every day at 08:30 HKT, automatically book a PolyU tennis court for the slot **7 days later**, preferring 19:30–20:30, falling back to 18:30–19:30 then 20:30–21:30. One slot per day, any court.

Booking system: <https://www40.polyu.edu.hk/starspossfbstud/secure/ui_make_book/make_book.do>

## Behavior

| Aspect | Decision |
|---|---|
| Schedule | Daily, every day of the week |
| Trigger time | 08:30:00 HKT (with sub-second precision via in-script sleep) |
| Target booking date | `today_HKT + 7 days` |
| Time slot priority | 1) 19:30–20:30  2) 18:30–19:30  3) 20:30–21:30 |
| Slots booked per run | Exactly one. Stop after first success. |
| Court preference | None — first available court in priority slot wins |
| Notification | None on success. GitHub Actions auto-emails on workflow failure (`exit 1`). |
| "All slots taken" handling | `exit 1` — treated as workflow failure to surface via email |

## Architecture

### Hosting

- **GitHub Actions**, private repository.
- Free tier (2000 min/month private; ~1–2 min/run = well under quota).
- Credentials stored in **GitHub Secrets**: `POLYU_USERNAME`, `POLYU_PASSWORD`. Injected as env vars; never logged.

### Cron Strategy

GitHub Actions cron is documented as unreliable (delays of 5–30 min during peak hours). To work around this:

- Workflow `cron: '20 0 * * *'` — fires at **00:20 UTC = 08:20 HKT**, 10 minutes early.
- Booker script reads system clock (forced to `Asia/Hong_Kong` via `TZ` env var), sleeps until `08:30:00.000 HKT`, then fires the first request.
- This way GitHub's cron jitter is absorbed by the early start, not by the booking window.

Workflow `timeout-minutes: 10` to prevent hung runners.

### Runtime Stack

- **Python 3.12** (already configured in `pyproject.toml` / `.python-version`)
- **Playwright** (Chromium, headless) — chosen over raw HTTP for robustness against CSRF tokens, JS-rendered tables, and session cookies. Cold start ~2–3 s on GH runner; acceptable for a single-user use case.
- **Standard library** for time/date/zoneinfo. No other heavy deps.

## Repository Layout

```
TennisBooking/
├── .github/workflows/book.yml   # Cron + Playwright setup
├── src/
│   ├── booker.py                # Main: login → search → select slot → submit
│   ├── config.py                # Slot priority, date offset, URLs, selectors
│   └── log.py                   # Structured logging to stderr
├── tests/
│   └── test_booker.py           # Offline unit tests (mocked Playwright)
├── docs/superpowers/specs/      # This document and future specs
├── pyproject.toml               # Adds playwright dep
├── .python-version
└── README.md                    # Deployment + Secrets setup guide
```

## Booking Flow (booker.py)

```
1. Read env: POLYU_USERNAME, POLYU_PASSWORD
2. target_date = (now in Asia/Hong_Kong).date() + timedelta(days=7)
3. Sleep until next 08:30:00.000 HKT (no-op if already past, but we shouldn't be)
4. Launch Chromium (headless, single browser context)
5. Navigate to make_book.do, log in
6. Navigate to Sports Facility, select Activity = Tennis, click Search
7. On the search results page, locate the row for target_date
8. For each slot in priority order [19:30, 18:30, 20:30]:
     a. Find an available cell for that slot (any court)
     b. If found:
          - Click the cell
          - Click "Next" → make_book_submit.do
          - Tick the "I hereby ..." checkbox
          - Click "Submit"
          - Verify confirmation page reached
          - Save screenshot of confirmation
          - exit 0
     c. If not found, try next priority slot
9. If no slot booked: log which courts were occupied per slot, exit 1
```

### Screenshot Artifacts

Every run uploads screenshots as a GitHub Actions artifact (key checkpoints: search results page, pre-submit page, confirmation/failure page). Retained per default GH artifact policy. Aids post-mortem when UI changes break selectors.

## Failure Modes & Exit Codes

| Condition | Exit code | Email? | Notes |
|---|---|---|---|
| Slot booked successfully | 0 | No | Screenshot saved as artifact |
| All 3 priority slots full | 1 | Yes | Body of log lists what was occupied |
| Login failure (wrong creds, system down) | 1 | Yes | Distinct log marker for triage |
| Selector not found (UI changed) | 1 | Yes | Retry once, then fail with screenshot |
| Network timeout | 1 | Yes | Single retry per page nav |
| Already booked (rule violation from server) | 1 | Yes | Server-side error message captured |

## Testing

- **Unit tests** (offline, fast):
  - Slot priority ordering
  - `target_date` calculation across HKT day boundary
  - `sleep_until` math (mocked clock with `freezegun`)
  - Log redaction (password never appears in formatted logs)
- **Integration test**: not in CI — booking is destructive and rate-limited. Manual smoke-test on day 1 of deployment.
- **Dry-run mode**: `--dry-run` flag walks the entire flow up to but not including the final Submit click. Used for selector verification on first deploy.

## Open Items — Verify in Implementation Phase

The following cannot be settled without logging into the live system. The skeleton will be written first, then a one-time browser session with real credentials will produce the concrete selectors:

1. HTML structure of the search results page (table layout, court column header, time row labels)
2. How an "available" vs "booked" cell is distinguished (CSS class? `disabled` attr? color only?)
3. Time format on the page (`19:30` vs `07:30 PM` vs `1930`)
4. Field names / IDs for the agreement checkbox and Submit button on `make_book_submit.do`
5. Whether a CAPTCHA, second-factor, or extra confirmation modal appears at any step
6. Whether the "1 week ahead" rule is hard-enforced by which dates are visible (likely yes — the day-7 cells only appear from 08:30 HKT on day 0)
7. Whether the system has a per-week or per-day booking quota that could cause server-side rejection on consecutive days

## Out of Scope (Explicit Non-Goals)

- Booking 2-hour back-to-back slots (user chose single-slot mode)
- Court preference / picking a specific court number
- Success notifications (only failure email via GH default behavior)
- Booking any sport other than tennis
- A frontend or web UI for managing the booker
- Race-condition optimization beyond the sleep-until-08:30 trick (not competing against other scripts; competing against humans clicking)
