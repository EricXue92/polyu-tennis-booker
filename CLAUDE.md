# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-purpose script that books a PolyU tennis court 7 days ahead, every
morning at 08:30 HKT. Runs as a GitHub Actions `workflow_dispatch` job
triggered daily by a Cloudflare Worker (entrypoint `book-tennis`, defined in
`pyproject.toml`).

## Commands

```bash
uv sync                                   # install deps (Python 3.12+, Playwright)
uv run playwright install chromium        # one-time browser install
uv run pytest                             # run all unit tests (offline, no network/browser)
uv run pytest tests/test_http_booker.py::test_happy_path_rank0_wins
uv run book-tennis --dry-run --skip-sleep # local end-to-end (needs POLYU_USERNAME/POLYU_PASSWORD)
uv run book-tennis --dry-run --skip-sleep --target-date 2026-09-19  # exercise a weekend (dual-account) rule
```

Manual workflow trigger (use after watchdog issue, or to test on a branch):

```bash
gh workflow run "Daily Tennis Booking" -f dry_run=false -f skip_sleep=true
gh workflow run "Daily Tennis Booking" --ref <branch> -f ...   # CF Worker only triggers main; use --ref for branch tests
gh workflow run "Daily Tennis Booking" -f dry_run=true -f skip_sleep=true -f target_date=2026-09-19  # verify a specific target date
gh run watch <id> --interval 15 --exit-status                  # block until done
```

`--dry-run` stops before any cell-click (it verifies login + bootstrap only).
`--skip-sleep` runs immediately instead of waiting until 08:30 HKT — and also
skips the warmup; after 08:30, `dry_run=true skip_sleep=false` exercises the
warmup path too, since every sleep is then 0.

Cloudflare Worker (trigger + watchdog): `cd infra/cloudflare-worker`, then
`npx wrangler deploy` / `npx wrangler tail`; PAT rotation and local cron
testing are in its `README.md`.

## Architecture

`src/booker.py:run` picks the active accounts for the target date
(`active_jobs`), does a Playwright login for each at 08:29 (concurrently, one
browser context per account), extracts session state (cookies + CSRFToken +
fbUserId) via `bootstrap_http_client`, closes the browser, warms each
account's connection pool (`warm_all`), sleeps to 08:30:00.000, and hands off
to `src/http_booker.py:book_via_http` once per account via `book_all`
(`asyncio.gather`; one `AccountResult` per account, exit 0 via `overall_rc`
only if every account booked).

That orchestrator **skips search** and runs two phases over the
`(SLOT_PRIORITY × TENNIS_FACILITIES)` candidate set. Phase 1 fires every
candidate's `cell_click()` concurrently (re-firing timed-out candidates if
none was ACCEPTED). Phase 2 groups ACCEPTED cells by `(start, end)` time-slot
preserving rank order, fires each group's `submit()` concurrently, and
**staggers** the groups. The winner is the lowest-rank SUCCESS across all
groups. The client (`src/http_client.py`) issues the POSTs (make_book.do,
make_book_submit.do) over raw httpx — no Playwright on the hot path.
(`PolyUHttpClient.search()` exists for diagnostics only.)

**History.** `docs/incidents.md` is the dated incident log; rules below cite
it as `[YYYY-MM-DD]` — read the cited entry before changing that rule. Design
rationale through 2026-08 is in `docs/superpowers/specs/` and `plans/`; later
changes (dual accounts, notify, cell-click retry) have no spec.

## Accounts and sites

`src/config.py` defines `Site` (staff `starspossfbns`, student
`starspossfbstud` — same host, different J2EE context root, identical endpoint
suffixes) and `Account` (site + credential env-var names + a
`slot_priority(target_date)` rule). `ACCOUNTS = (STAFF_ACCOUNT,
STUDENT_ACCOUNT, STUDENT2_ACCOUNT)`. An account whose rule returns `()` sits
the run out. Each `PolyUHttpClient` is bound to one `Site` and derives its
URLs and Referer headers from it — never hardcode a context root in the
client.

- **Staff** (`POLYU_USERNAME`/`POLYU_PASSWORD`): the daily booker, rule
  `slot_priority_for` (see weekday adjustments below).
- **Student** (`POLYU_STUDENT_USERNAME`/`POLYU_STUDENT_PASSWORD`): weekends
  only, rule `student_slot_priority_for`. The owner wants two consecutive
  weekend hours, one per account, so the evening is split by parity: staff
  takes the **even** hours (18:30 → 20:30), student the **odd** hours (17:30 →
  19:30 → 21:30), both on both courts. Both fire at 08:30 blind to each
  other, so this static split is the only thing preventing overlap — never
  give either account a weekend fallback on the other's hours.
- **Student2** (`POLYU_STUDENT2_USERNAME`/`POLYU_STUDENT2_PASSWORD`, student
  site): one-off, rule `student2_slot_priority_for`. Active only for target
  dates in `STUDENT2_TARGET_DATES` (2026-09-28, 09-30, 10-02), trying 20:30 →
  21:30. Staff still runs its weekday rule those days, so both can land on
  20:30 (different courts); nothing prevents that. Empty the set after
  2026-10-02.
- **Student result email.** Accounts with `notify_result=True` (only
  `student`) get their outcome emailed after every run they take part in;
  weekday runs send nothing. `src/notify.py` uses Gmail SMTP
  (`SMTP_USERNAME`/`SMTP_PASSWORD` = app password; recipient
  `NOTIFY_EMAIL_TO`, default `SMTP_USERNAME` — the repo is public, never
  hardcode the address). It runs strictly after `book_all`, is best-effort
  (failure is a WARNING), and must never change the exit code. A dry run
  sends a `[DRY RUN]` email — that is how to verify the SMTP secrets.
- **Per-account isolation.** A failed login or crash in one account is logged
  and the others still book; the run exits 1 afterwards. Each account logs
  through its own `build_logger(..., session_id=name)` so its password is
  redacted.

## Invariants (do not break these)

Timing:

- **Slots open at EXACTLY 08:30 HKT.** Never let the booker run earlier: no
  `skip_sleep=true` by default, don't remove the
  `asyncio.sleep(seconds_until_hkt_time(...))` calls in `booker.run`, don't
  lower `TRIGGER_TIME_HKT`. Early sees no slots; late loses popular ones.
- **A Cloudflare Worker triggers the workflow** (`infra/cloudflare-worker/`)
  at 07:30 HKT via `workflow_dispatch`; GH's scheduled cron proved
  unreliable, so book.yml has **no `schedule:` block** `[2026-05-10]`. The
  60-min lead absorbs runner queue delays. A second Worker cron at 08:35
  opens a GitHub issue (emails the owner) if the day has no successful run.
- **`skip_sleep` default MUST stay `false` in book.yml.** The Worker
  dispatches with no inputs, so defaults apply — `true` would book before
  slot-open and silently fail. The Worker also hardcodes `ref: "main"`.
- **Three-phase sleep — don't collapse it or skip the warmup.** 08:29:00
  login + bootstrap, close browser → 08:29:58 `client.warmup(n=candidates)`
  (one warm connection only helps the first concurrent POST) → 08:30:00.000
  fire. A cold first POST at 08:30 once took 5.5s `[2026-06-05]`.
- **Warmup never delays the trigger.** `booker.warm_all` waits only until
  08:30:00; stragglers keep running (not cancelled — they still add warm
  sockets) and the fire goes out on time, `max_connections=16` leaving room
  for cold connections. Don't start the warmup earlier instead: PolyU drops
  idle keepalives after ~5s, so `WARMUP_LEAD_SECONDS` must stay small
  `[2026-09-20]`.

Hot path:

- **No search.** PolyU's Search takes ~4.5s server-side. `book_via_http`
  fabricates `AvailableSlot`s from the candidate set and posts straight to
  make_book.do; a nonexistent facility ID just returns OCCUPIED
  `[2026-06-03]`.
- **Cell-click results are per-candidate.** One OCCUPIED / TRANSIENT / FATAL
  never aborts its siblings.
- **Cell-click timeouts are retried, never final.** A round with no ACCEPTED
  but some ERROR_TRANSIENT re-fires just those candidates (`CELL_RETRY_*` in
  `http_booker.py`: rounds for up to 45s, 15s per request, paced ≥1s). No
  retry once anything is ACCEPTED — a ready submit must not wait on a slow
  sibling. A timeout says nothing about the slot `[2026-09-20]`.
- **Submits: concurrent within a group, groups staggered, never serial**
  (`SUBMIT_STAGGER_SECONDS = 2.5`: group N+1 launches when earlier groups
  settle or the stagger elapses). Serial submits let one hung rank-0 lock out
  its siblings `[2026-06-18]`; serial groups lost 7 runs `[2026-08-19]`.
- **The winner is chosen by rank, never by arrival order** — groups overlap,
  so a fast 19:30 answer must not beat a slower 18:30 one. Launching stops
  early once a settled group holds a SUCCESS (don't burn the daily quota on a
  fallback) or a FATAL with no SUCCESS (auth presumed dead).
- **Two timeout budgets** (`PolyUHttpClient(timeout=6.0,
submit_timeout=20.0)`). `timeout` guards cell_click/warmup: 150–300ms warm,
  and gathered, so one hang stalls the whole submit phase (retry rounds get
  15s — nothing is queued behind them). `submit_timeout` guards
  make_book_submit.do, which legitimately takes 4–6s+ at 08:30; connect stays
  on the short budget. Aborting a submit rolls it back server-side, so
  waiting always beats giving up `[2026-08-19]`.
- **Double-booking is prevented by PolyU's quota, not by us** (one booking
  per day; a surplus commit gets the quota page `[2026-08-29]`). The
  multi-SUCCESS WARNING stays as belt-and-braces. Deliberately no auto-cancel
  path.
- **Submit result is decided from one response — no waiter race.** Location
  `make_book_result.do` ⇒ SUCCESS; "occupied" (case-insensitive) in the body
  or any `make_book*` redirect ⇒ OCCUPIED `[2026-06-07]`; anything else ⇒
  `ERROR_*`. Every `ERROR_*` from `cell_click`/`submit` logs status +
  Location + body_len + preview + markers + context, with `preview`/`context`
  built from the page's **visible text** (`_visible_text`, `_diag_context`),
  never raw HTML — PolyU error pages are ~30 KB of chrome around one line
  `[2026-09-06]`.

Hygiene:

- **Password redaction.** All logging goes through `src/log.py:build_logger`
  (the filter replaces the password with `***` before any handler) — no
  `print()`, no root logger. Playwright errors can quote field values.
- **Login verification.** After submitting credentials, `login()` raises
  `LoginFailed` if the URL still contains `loginhome` or the username field
  is still present — wrong credentials must not masquerade as a downstream
  selector timeout.
- **Exit codes drive notification.** Exit 0 = every active account booked
  (silent). Exit 1 = any account got no slot or errored — GitHub emails the
  workflow owner. Staff deliberately has no success notification; the
  `student` result email is separate and never feeds the exit code.
- **Tests are offline.** Everything in `tests/` uses fakes (e.g.
  `_FakeClient`) — no network, no Playwright. Don't add live integration
  tests; verify with `--dry-run` against the real site.
- **CI sets `TZ: Asia/Hong_Kong`** — `dates.py:now_hkt()` assumes it. Don't
  remove it from `.github/workflows/book.yml`.

## Reading a failed run

Check latencies before suspecting the code:

- **Every candidate OCCUPIED within ~0.5s of 08:30** → the courts were
  reserved ahead of the public release; nothing to fix. A slot lost to
  competition answers after normal submit latency (~3.8–6s). Cell-click
  ACCEPTED is only session state, not availability `[2026-09-19]`.
- **`submit unexpected … ERROR_FATAL` next to a SUCCESS** → PolyU's quota page
  rejecting the surplus commits; expected on every healthy day, because 18:30
  answers at ~3.8s, after the stagger has already launched 19:30. The quota
  check counts a sibling commit still in flight, so the rejection can arrive
  _before_ the SUCCESS `[2026-09-17]`.
- **`cell_click transport error: ReadTimeout` + `retrying them (attempt N)`**
  → PolyU was slow; the retry path is working as designed `[2026-09-20]`.
- **Open:** a 29640-byte ERROR_FATAL page that is _not_ the quota page
  (29715 bytes) and whose message the diagnostics don't capture yet — see
  `[2026-09-17]` before building on any assumption about it.

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
- **Dry-run does not prove PolyU still accepts our cell-click POST shape.**
  To live-test that: drop `dry_run`, set `SLOT_PRIORITY` to a known-free
  off-peak window, and run before 08:30 HKT.

## Tuning knobs

- Slot preferences: `SLOT_PRIORITY` in `src/config.py` (tuple of
  `(start, end)`, tried in order). Weekdays run 18:30 → 19:30 → 20:30; the
  third rung is cheap insurance on contested evenings `[2026-09-16]`.
- Trigger time: `TRIGGER_TIME_HKT` in `src/config.py`.
- Days-ahead window: `DAYS_AHEAD` in `src/dates.py`.
- Submit stagger / cell-click retry: `SUBMIT_STAGGER_SECONDS`, `CELL_RETRY_*`
  in `src/http_booker.py`.

## Weekday-specific adjustments

`config.slot_priority_for(target_date)` adjusts `SLOT_PRIORITY` per weekday
before sessions are created. When every account's rule returns `()`, `run()`
short-circuits with exit 0 (no sleep, no Playwright launch) — the watchdog
treats the day as accounted for and does not open an issue. Currently:

- **Tuesday is a rest day.** `target_date.weekday() == 1` is in
  `_REST_WEEKDAYS` (owner's preference).
- **Weekends split hours between the two accounts.** Saturday/Sunday targets
  return `_STAFF_WEEKEND_SLOTS` (18:30, 20:30) for staff and
  `_STUDENT_WEEKEND_SLOTS` (17:30, 19:30, 21:30) for student — see "Accounts
  and sites" for why the parity split matters.

Add new rest weekdays to `_REST_WEEKDAYS`. For partial exclusions (some slots
skipped but the day still booked), reintroduce a frozenset of `(start, end)`
tuples and filter `SLOT_PRIORITY` against it in `slot_priority_for`.
