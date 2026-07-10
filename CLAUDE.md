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
uv run pytest tests/test_http_booker.py::test_happy_path_rank0_wins
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

## Architecture

The booking flow spans three files. `src/booker.py:run` does Playwright login
at 08:29, extracts session state (cookies + CSRFToken + fbUserId) via
`bootstrap_http_client`, closes the browser, sleeps to 08:30:00.000, and hands
off to `src/http_booker.py:book_via_http`. That orchestrator **skips search**
and runs two phases over the `(SLOT_PRIORITY × TENNIS_FACILITIES)` candidate
set: phase 1 fires every candidate's `cell_click()` concurrently via
`asyncio.gather`; phase 2 groups ACCEPTED cells by `(start, end)` time-slot
preserving rank order, fires each group's `submit()` concurrently, and walks
groups sequentially in priority order. The client (`src/http_client.py`)
issues the booking POSTs (make_book.do, make_book_submit.do) over raw httpx —
no Playwright, no search on the hot path. (`PolyUHttpClient.search()` still
exists for diagnostic use but isn't called in production.)

Incident history and full design rationale live in `docs/superpowers/specs/`
and `docs/superpowers/plans/` — read the most recent files there before
substantive changes.

## Invariants (do not break these)

- **PolyU releases 7-days-ahead slots at EXACTLY 08:30 HKT.** Never let the
  booker run before 08:30 (no `skip_sleep=true` by default, don't remove the
  `sleep_until_hkt` call, don't lower `TRIGGER_TIME_HKT`). Early sees no
  slots; late loses popular slots.
- **An external Cloudflare Worker triggers the workflow**
  (`infra/cloudflare-worker/`) at 07:30 HKT via `workflow_dispatch` — GH
  Actions' scheduled cron proved unreliable, so the workflow has **no
  `schedule:` block**. The 60-min lead absorbs runner queue delays (observed
  up to 35 min); the booker sleeps to 08:30:00.000 regardless of start time.
  A second Worker cron at 08:35 opens a GitHub issue (auto-emails the owner)
  if no successful run exists for the day.
- **`skip_sleep` default MUST stay `false` in book.yml.** The CF Worker
  dispatches with no inputs, so defaults apply — `true` would book before
  slot-open and silently fail. The Worker also hardcodes `ref: "main"`.
- **Three-phase sleep — do not collapse or skip the warmup.** Sleep to
  08:29:00 → Playwright login + `bootstrap_http_client`, close browser; sleep
  to 08:29:58 → `client.warmup(n=len(candidates))` (servers drop idle
  keepalives within 15–30s, and one warm connection only helps the first
  concurrent POST, so warmup primes TCP+TLS for every candidate); sleep to
  08:30:00.000 → fire. Landing every cell-click on a warm connection at
  exactly 08:30 is the entire point; a cold handshake costs ~5s and loses the
  run.
- **No search on the hot path.** PolyU's Search endpoint takes ~4.5s
  server-side — long enough to lose every desired slot. `book_via_http`
  fabricates `AvailableSlot`s from the candidate set and goes straight to
  `make_book.do`. Candidates for facility IDs that don't exist on the target
  date just return OCCUPIED (one wasted POST, same code path).
- **Parallel semantics.** Cell-click results (OCCUPIED / ERROR_TRANSIENT /
  ERROR_FATAL) are **per-candidate** — one FATAL does not abort the run.
  Within a submit group: any SUCCESS wins (lowest rank preferred);
  OCCUPIED / TRANSIENT advance to the next group; FATAL with no SUCCESS
  sibling aborts remaining groups (auth presumed dead), but a parallel
  SUCCESS in the same group overrides the FATAL. Time-major priority is
  preserved (all 18:30 attempts finish before any 19:30 starts); facility
  ordering within a timeslot is sacrificed. Do not reintroduce serial
  submits — one hung rank-0 submit once locked out all its siblings.
- **Double-booking inside a parallel group is accepted.** If two submits in
  one group both SUCCEED, `book_via_http` logs a WARNING listing the surplus
  booking for manual cancel. Deliberately no auto-cancel path (scope creep
  for a case that has never occurred in contested windows).
- **HTTP client timeout is 6s** (`PolyUHttpClient(timeout=6.0)`). Floor set
  by the slowest observed legitimate SUCCESS submit (5.3s); ceiling bounds
  the damage from PolyU-side hangs. Warm-pool cell-clicks run 150–300ms. If
  a legitimate submit ever needs >6s, expect false TRANSIENTs and reopen.
- **Submit result is decided from one response — no waiter race.** Location
  header `make_book_result.do` ⇒ SUCCESS; "occupied" (case-insensitive) in
  body or any `make_book*` redirect ⇒ OCCUPIED (the broad match covers a
  known `302 → make_book.do` rebound that a narrow match misclassified as
  FATAL); anything else ⇒ ERROR_*. `cell_click` and `submit` log body
  diagnostics (status + Location + body_len + preview + markers) on every
  ERROR_* so anomalies are root-causeable from CI logs alone.
- **Password redaction.** All logging must go through
  `src/log.py:build_logger` (filter replaces the password with `***` before
  any handler) — no `print()`, no root logger. Playwright errors can quote
  field values.
- **Login verification.** After submitting credentials, `login()` checks the
  URL still contains `loginhome` or the username field is still present and
  raises `LoginFailed` — wrong credentials must not masquerade as a
  downstream selector timeout.
- **Exit codes drive notification.** Exit 0 = booked (silent). Exit 1 = no
  slot or any error — GitHub emails the workflow owner on failure. There is
  deliberately no success-notification path.
- **Tests are offline.** Everything in `tests/` uses fakes (e.g.
  `_FakeClient`) — no network, no Playwright. Don't add live integration
  tests; verify with `--dry-run` against the real site.
- **CI sets `TZ: Asia/Hong_Kong`** — `dates.py:now_hkt()` and the cron
  comments assume it. Don't remove it from `.github/workflows/book.yml`.

## Maintenance hooks (when PolyU changes)

- **Selectors are externalized** in `src/config.py:Selectors`. Symptom:
  `Selector ... is not configured` or a Playwright timeout on a specific
  element. Fix: re-run `scripts/discover_selectors.py` (needs
  POLYU_USERNAME/POLYU_PASSWORD), inspect `artifacts/*.html`, update the
  dataclass. `require()` raises on any `PENDING_DISCOVERY` sentinel.
- **HTTP request shapes are externalized** via `scripts/capture_http.py`,
  which runs the flow under Playwright with request/response hooks and dumps
  `artifacts/http_trace.json` (credentials redacted). Symptom:
  `PolyUHttpClient` 4xxs or unexpected response shape. Fix: re-capture and
  update the templates in `src/http_client.py`. Requires a known free
  off-peak slot — see the script's header comment. CI uploads any
  `artifacts/` content if present; the booker itself creates no files.
- **Dry-run stops before any cell-click** — it verifies login + bootstrap
  only, NOT that PolyU still accepts our cell-click POST shape. To live-test
  that: drop `dry_run`, set `SLOT_PRIORITY` to a known-free off-peak window,
  and run before 08:30 HKT.

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
