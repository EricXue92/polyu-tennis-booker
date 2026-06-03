# HTTP-replay booking flow (drop Playwright from the hot path)

**Date:** 2026-06-03
**Status:** Proposed
**Author:** XUE (with Claude)

## Background

On competitive booking days (Wed/Thu/Sat/Sun targets), the booker is
losing the slot-open race despite hitting `Search` within 35ms of
08:30:00.000 HKT. Today's run (target 2026-06-10, a Wednesday) was
typical:

| Time (HKT) | Event |
|---|---|
| 08:30:00.035 | All 3 sessions click `Search` |
| 08:30:04.42 | Search results render (PolyU server lag ~4.4s) |
| 08:30:04.43 | First session clicks available cell |
| 08:30:06.00 | First session clicks `Next` |
| 08:30:06.70 | First session ticks agreement checkbox |
| 08:30:06.77 | First session clicks `Submit` |
| 08:30:07.17 | "Facility is occupied" banner → all 3 priority slots lost |

The previous spec
(`2026-05-31-click-through-latency-optimizations-design.md`) shaved
~1.1s off Playwright UI overhead and noted that PolyU's ~4s Search
render and the underlying Playwright click-through chain are the next
bottleneck — leaving "reverse-engineering PolyU's form POSTs to skip
the UI" as an explicit follow-up. That follow-up is this spec.

Empirically the winners are clearly submitting within ~1s of slot-open.
With Playwright the absolute floor of the current architecture is
T+5–6s (PolyU's Search alone is ~4s, plus three more browser-rendered
pages). We need to leave Playwright off the hot path.

Recent 10-day pattern by target weekday:
- Mon, Fri: ✅ (low competition)
- Tue: rest day, no-op (no booking attempted)
- Wed, Thu, Sat, Sun: ❌ — all 3 priority slots taken inside the click-through window

## Goal

Replace the per-slot Playwright click-through with raw HTTP requests so
the final `Submit` POST lands within ~1s of the Search response
(target T+5.0s overall, vs the current T+6.77s). The fundamental ~4s
PolyU server latency on Search remains untouched — the win comes from
eliminating Playwright's per-click rendering wait and `wait_for_url` /
`wait_for_load_state` overhead between Search and Submit.

**Success criteria:**
- Mon/Fri runs continue to succeed (no regression).
- Wed/Thu/Sat/Sun success rate ≥ 75% over a 10-week observation window
  (vs current ~25%).
- The capture script remains usable for re-discovering request shapes
  after PolyU UI changes (the HTTP analogue of
  `scripts/discover_selectors.py`).
- Submit POST lands at ≤ T+5.5s on the typical run (measured from
  08:30:00.000 HKT).

## Non-goals

- Login is **not** rewritten in HTTP. Login flow uses Playwright and
  hands cookies to `httpx`. Doing login over HTTP is an optional later
  optimization; if `capture_http.py` reveals login is trivial form-POST
  it may follow, but it is not in scope here.
- Multiple accounts / family-account farming.
- Expanding `SLOT_PRIORITY` to add fallback windows (16:30, 20:30,
  weekend mornings) — user explicitly prefers the existing 17:30 /
  18:30 / 19:30 set. If the HTTP rewrite still fails on competitive
  days after 2 weeks of live data, this is the natural next step but
  out of scope here.
- Detecting / bypassing PolyU anti-bot. We mimic Chrome headers but do
  not actively evade detection beyond that.

## Architecture

```
            ┌──────────────────────────────┐
            │ run() in src/booker.py       │
            │  - login via Playwright      │
            │  - extract cookies           │
            │  - close browser             │
            │  - sleep_until_hkt(08:30)    │
            └─────────────┬────────────────┘
                          │ cookies, target_date, slots
                          ▼
            ┌──────────────────────────────┐
            │ src/http_booker.py           │
            │  book_via_http(client,       │
            │                slots, ...)   │
            │  1. client.search(date)      │
            │  2. for slot in priority:    │
            │       client.try_book(slot)  │
            │       if SUCCESS: break      │
            └─────────────┬────────────────┘
                          │
                          ▼
            ┌──────────────────────────────┐
            │ src/http_client.py           │
            │  PolyUHttpClient (httpx)     │
            │  - search(date)              │
            │  - try_book(slot)            │
            └──────────────────────────────┘
```

### Module-by-module

**`scripts/capture_http.py`** (new, one-shot tool)
- Reuses `src.booker.login` + `prepare_search` + the existing
  click-through flow.
- Attaches `page.on("request")` + `page.on("response")` to dump every
  HTTP exchange under `*.polyu.edu.hk/starspossfbstud/*` to
  `artifacts/http_trace.json` with: URL, method, request headers
  (password / cookie values redacted via the same `log.py` filter
  approach), form-encoded body (password redacted), response status,
  response Content-Type, response body (HTML body truncated to ~10KB
  for inspection, JSON kept whole).
- Requires running locally with valid credentials against a known
  off-peak target slot, OR with `--no-submit` to skip the final POST
  but still record the request shape from the cell-click + Next +
  checkbox steps. Documented in CLAUDE.md alongside
  `discover_selectors.py`.

**`src/http_client.py`** (new)
- `PolyUHttpClient` wraps an `httpx.AsyncClient` constructed with:
  - Cookies copied verbatim from `BrowserContext.cookies()` (preserves
    `httponly`, `samesite`, domain scoping).
  - Headers mimicking the Playwright Chromium build: `User-Agent`,
    `Accept`, `Accept-Language`, `Accept-Encoding`, `Sec-Fetch-*`. The
    captured trace dictates the exact values.
  - `follow_redirects=True`, `timeout=10s`.
- Methods (final signatures driven by capture):
  - `async def search(self, target_date: date) -> SearchResult` —
    POST/GET to the Search endpoint, parse the response (HTML table
    rows for slot cells, or JSON if PolyU returns JSON; capture
    decides) into a structure that maps `(start, end)` → cell token /
    URL params.
  - `async def try_book(self, target_date: date, slot: tuple[time, time]) -> BookingResult` —
    sequence cell-click + Next + checkbox + Submit POSTs. Returns
    `SUCCESS`, `OCCUPIED` (recognized failure banner / status), or
    `ERROR`. Compresses to the fewest round-trips PolyU allows; the
    capture trace determines whether cell-click and Next can be a
    single POST, etc.
  - All requests log start/finish times via the password-redacting
    logger from `src/log.py`. The password and any captured CSRF
    tokens are added to the redaction list.
- Selectors for HTML response parsing live in `src/config.py` next to
  the existing `Selectors` dataclass (or in a new `HttpEndpoints`
  dataclass — TBD on capture). Same `PENDING_DISCOVERY` sentinel
  pattern.

**`src/http_booker.py`** (new, replaces `src/parallel_runner.py`)
- `async def book_via_http(client, target_date, slots, dry_run) -> int`
  is the new top-level orchestrator.
- Calls `client.search(target_date)` once.
- Iterates `slots` in priority order, calls `client.try_book(slot)`,
  returns 0 on first `SUCCESS`, 1 if all fail.
- `dry_run=True` skips the final Submit POST inside `try_book` (cell
  click and Next still fire to validate the trace).
- Per-slot screenshots are gone — replaced by `artifacts/http_trace.json`
  for the actual run (same format as `capture_http.py` produces),
  uploaded by CI on every run. This becomes the new debugging artifact.
- If, on inspection of `http_trace.json`, PolyU is found to permit
  concurrent `try_book` from the same session, this orchestrator can
  switch to `asyncio.gather` + first-SUCCESS-wins. Default is serial
  in priority rank order — the same semantic as today's coordinator.

**`src/booker.py:run`** (modified)
- Drops `book_parallel` import; calls `book_via_http`.
- Single login (no `login_lock` — only one Playwright context).
- After login: `cookies = await context.cookies()`, `await
  browser.close()`, `client = PolyUHttpClient.from_cookies(cookies)`.
- `prepare_search` is gone — its only purpose was to leave the
  Playwright form ready to fire Search; the HTTP `search()` does that
  in one POST. Keep its tests if they cover dropdown selection;
  delete them otherwise.
- The two-phase sleep collapses to one: log in immediately at startup
  (still earlier than 08:30 because the CF Worker triggers at 07:30,
  giving 60+ minutes of slack), then sleep to 08:30:00.000, then call
  `book_via_http`. The "login is intentionally before 08:30" rule in
  CLAUDE.md still holds.

**`src/parallel_runner.py`** (removed)
- The N-session-per-slot model existed to amortize Playwright's
  per-page-render cost across the click-through window. In HTTP that
  cost is gone — one session can attempt N slots faster than N
  Playwright contexts could attempt one slot each. The single-dequeuer
  coordinator's role (serialize Submit in priority order) is preserved
  trivially by the for-loop in `book_via_http`.

**`tests/`** (changes)
- Add `tests/test_http_client.py` — offline unit tests using `respx`
  (httpx test helper) or a hand-rolled `httpx.MockTransport`. Fixtures
  are minimal replicas of the captured responses (one per endpoint,
  trimmed). Tests cover: search parses the slot table correctly,
  try_book happy path returns SUCCESS, try_book occupied-banner
  response returns OCCUPIED, try_book network error returns ERROR.
- Keep `tests/test_slot_finder.py` if `pick_slot` survives; otherwise
  delete it with `pick_slot`.
- Tests stay offline. No live PolyU integration tests.

## Timing budget (target)

| Phase | Current | Target |
|---|---|---|
| Sleep → 08:30:00 fire Search | T+0.03s | T+0.03s |
| Search response (PolyU server) | T+4.4s | T+4.4s (unchanged) |
| cell-click → Next → checkbox → Submit | T+4.4s → T+6.77s | T+4.4s → T+5.0s |
| Submit response | T+7.17s | T+5.5s |

If `capture_http.py` reveals that `cell-click` and `Next` can be
fused into a single POST (or skipped entirely because PolyU's Submit
endpoint accepts the slot token directly), the budget tightens
further. Conversely, if PolyU requires each step as a separate POST
with view-state token chaining, the budget loosens by ~150ms per
extra round-trip but still beats T+6.77s.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| PolyU uses a one-shot CSRF / Struts view-state token per step | `capture_http.py` records the request flow with response bodies; `try_book` parses each response for the next step's token. Same pattern as a browser. |
| PolyU detects non-browser UA / missing headers | Copy the captured Chromium UA + `Accept-*` + `Sec-Fetch-*` headers verbatim. Use Playwright's actual UA, not a hardcoded one. |
| Cookies lost in Playwright→httpx transfer (`httponly`, domain mismatch) | `httpx.Cookies` accepts the full `BrowserContext.cookies()` shape. After construction, the client GETs `make_book.do` as a sanity check — if it 302s back to login, raise distinctly. |
| Login itself relies on JS (meta-refresh + token injection) | Login stays in Playwright. This is the hybrid model's whole point. |
| Same-session concurrent POSTs cause PolyU to error / mis-book | Default to serial in priority order (today's behavior). Only switch to concurrent after the trace + a manual test confirms safety. |
| Captured trace goes stale when PolyU updates the UI | `capture_http.py` is reusable — re-run it, diff the JSON, update `PolyUHttpClient`. Documented in CLAUDE.md. |
| Dry-run smoke test still time-dependent (off-peak slot required to reach Submit) | Same constraint as today. Documented in CLAUDE.md. |
| Login single point of failure (no N-session "retry") | Keep one in-process login retry on `LoginFailed` (new). The watchdog issue at 08:35 HKT still fires on hard failure. |

## Implementation phases

1. **Capture (one-time, local)** — implement `scripts/capture_http.py`,
   run it locally targeting a known-free off-peak slot to record a full
   successful booking trace. Inspect `http_trace.json` to confirm the
   request shape (endpoints, tokens, body fields, response markers for
   SUCCESS / OCCUPIED).
2. **Client (offline TDD)** — implement `src/http_client.py` against
   `respx`-mocked responses derived from the captured trace. Tests
   green.
3. **Orchestrator** — implement `src/http_booker.py`. Replace
   `book_parallel` call site in `src/booker.py:run`. Remove
   `src/parallel_runner.py` and its tests.
4. **Local validation** — `uv run book-tennis --dry-run --skip-sleep`
   locally, verify the request trace in `artifacts/http_trace.json`
   matches expectations and no step errors. Pick an off-peak slot or
   run before 08:30 HKT so probe succeeds.
5. **Merge + observe** — merge to main. Monitor 1 week of live runs;
   compare success rate vs the 10-day baseline above.

## Documentation updates (post-merge)

- `CLAUDE.md` — rewrite the "Architecture" bullets that describe the
  parallel-sessions model. Update the "Two-phase sleep" bullet (now
  one-phase). Replace "Race window is probe→Submit" timings with the
  new HTTP figures. Add a "Capturing HTTP request shape" bullet next
  to the "Selectors are externalized" bullet, referencing
  `scripts/capture_http.py`.
- `README` (if it exists) — same.

## Rollback

If the HTTP rewrite underperforms in live runs (success rate <
baseline), `git revert` the integration commit and the booker returns
to the parallel-Playwright architecture. `src/http_client.py` and the
capture script can stay as dead code in case we resume. Cloudflare
worker, workflow YAML, and watchdog logic are unaffected.

## Open questions

- Resolved at capture: does PolyU use a per-step view-state token, or
  is the cookie sufficient?
- Resolved at capture: can multiple `try_book` attempts run
  concurrently from one session?
- Resolved at capture: is the Search response HTML (needs parsing) or
  JSON (clean)?

All three answers fall out of running `capture_http.py` once. The
client implementation only starts after the trace is in hand.
