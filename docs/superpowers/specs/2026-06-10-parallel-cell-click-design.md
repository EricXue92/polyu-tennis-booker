# Parallel cell-click + classification fix — design

**Date:** 2026-06-10
**Status:** Approved (brainstorming complete, awaiting plan)
**Supersedes nothing.** Builds on `2026-06-03-http-replay-booking-design.md`.

## Context

Past 14 days (2026-05-26 → 2026-06-09): **5 success / 9 failure**. Two distinct failure modes:

**Type A — all candidates OCCUPIED** (e.g. 2026-06-04, 06-05).
First POST at 08:30:00 paid a ~5.5s cold TLS handshake; by the time it landed, all four
candidate slots had been taken by other users. Mitigated by commit `f5a396e` (2026-06-05),
which added a single-connection TLS warmup ~2s before the trigger.

**Type B — first response misclassified as ERROR_FATAL, run aborts** (e.g. 2026-06-07, 06-09).
Even *after* warmup, the first POST still took ~6s. The response shapes returned were:

- **2026-06-07:** submit returned `302` → `Location: .../make_book.do` (no `_submit`).
  `try_book` only matches `"make_book_submit" in submit_location` for OCCUPIED, so this
  fell through to ERROR_FATAL and aborted the run before candidates 2–4 were tried.
- **2026-06-09:** submit returned `status=200, Location=''`. Body unknown — current logging
  records only status + Location, not body length or content markers, so we cannot
  retroactively determine whether the slot was OCCUPIED, a quota response, or something else.

Two compounding problems behind Type B:

1. **Classification bug.** Submit-stage OCCUPIED detector is narrower than cell-click's
   (`make_book_submit` only, vs cell-click's broader `make_book`). A `302 → make_book.do`
   on submit semantically means "your booking failed, here's the listing" — i.e. OCCUPIED.
2. **Serial bookings under a ~6s first-POST tax cannot survive races.** Even with a
   working warmup, the first POST consistently takes 5–6s at the 08:30:00 slot-open moment
   (likely a single warm TLS connection in the pool; httpx opens new connections for
   concurrent POSTs that each pay a fresh handshake). Serial 4 candidates ≈ 6+1+1+1s = ~9s,
   wide enough for human users to grab every preferred slot.

## Goals

1. Fix the submit-stage OCCUPIED misclassification so a 302 → `make_book.do` advances to the
   next candidate instead of aborting.
2. When the booker hits an unexpected response shape, capture enough body diagnostics
   to root-cause it from CI logs alone (no live reproduction needed).
3. Eliminate the serial-attempt latency penalty. Fire all 4 cell-clicks concurrently so the
   `~6s first POST` is paid in parallel across candidates, not stacked.
4. Preserve strict priority semantics: submit always runs sequentially, always
   in priority order, never possible to book rank 3 when rank 0 also succeeded.

## Non-goals

- Multiple-account / parallel-session orchestration (see `2026-05-20-parallel-booking-sessions-design.md` — out of scope here).
- Expanding the candidate set beyond the current `SLOT_PRIORITY × TENNIS_FACILITIES`.
- Changing pre-login, bootstrap, or sleep timing.
- Verifying the *root cause* of the 5–6s first-POST latency. We architect around the
  symptom; root cause may surface from improved diagnostics over the following week.

## Architecture overview

### Today
```
run() → login → bootstrap → sleep → warmup(1 GET) → sleep
      → book_via_http:
          for slot in priority_order:
              result = try_book(slot)   # cell_click + submit, sequential
              if SUCCESS: return 0
              if OCCUPIED: continue
              if ERROR_*: abort
```

### After this change
```
run() → login → bootstrap → sleep → warmup(N concurrent GETs) → sleep
      → book_via_http:
          # Phase 1: fire all cell_clicks in parallel
          cell_results = await asyncio.gather(
              *(client.cell_click(s) for s in candidates)
          )
          # Phase 2: submit ACCEPTED candidates in priority order
          for slot, cr in zip(candidates, cell_results):  # priority order preserved
              if cr is not ACCEPTED: continue
              br = await client.submit(slot)
              if br is SUCCESS: return 0
              if br is OCCUPIED or ERROR_TRANSIENT: continue
              if br is ERROR_FATAL: abort   # auth lost — remaining submits will fail
          return 1
```

`N = len(candidates)` (currently 4). Warmup primes one TLS connection per upcoming
concurrent POST.

## Section A — Classification fix + diagnostic logging

### A.1 Submit OCCUPIED detection bug fix

In `src/http_client.py` `try_book` (after the split: `submit`), change L394-398 from:

```python
if submit_resp.status_code in (200, 302) and (
    "Facility is occupied" in (submit_resp.text or "")
    or "make_book_submit" in submit_location
):
    return BookingResult.OCCUPIED
```

to:

```python
if submit_resp.status_code in (200, 302) and (
    "occupied" in (submit_resp.text or "").lower()
    or "make_book" in submit_location
):
    return BookingResult.OCCUPIED
```

Two changes:

1. `make_book_submit` → `make_book` (matches both `.do` and `_submit.do`). Safe because
   `make_book_result` (SUCCESS) is checked earlier and returns before reaching this branch.
2. `"Facility is occupied"` exact match → `"occupied"` lowercase substring. Guards against
   PolyU rewording the banner.

Apply the same case-insensitive `"occupied"` substring relaxation to the **cell_click**
OCCUPIED detector (L324-328) for consistency — currently it also uses the case-sensitive
`"Facility is occupied"` literal. The `make_book` Location check there is already broad.

### A.2 Body diagnostic logging on ERROR_*

Every WARNING in `cell_click` and `submit` for unexpected response shapes gains a body
fingerprint. New helper:

```python
_DIAG_MARKERS = (
    "occupied", "quota", "exceeded", "logout", "expired",
    "successfully", "session", "denied", "invalid", "error",
)

def _diag_markers(body: str) -> list[str]:
    """Return which marker substrings appear in body (case-insensitive)."""
    if not body:
        return []
    low = body.lower()
    return [m for m in _DIAG_MARKERS if m in low]
```

WARNING log shape:

```
submit unexpected (stage=submit, status=200, location='', body_len=4821,
preview='<!DOCTYPE html><html>...', markers=['session', 'expired']) -> ERROR_FATAL
```

`preview` = first 300 chars of body, whitespace collapsed.
Applied on every fall-through to `_classify_http_error`.

## Section B1 — Parallel cell-click + priority-ordered submit

### B1.1 Split `try_book` into two coroutines

```python
class CellOutcome(enum.Enum):
    ACCEPTED        = enum.auto()  # 302 → make_book_submit; slot now held server-side
    OCCUPIED        = enum.auto()  # slot already taken
    ERROR_TRANSIENT = enum.auto()  # 5xx / network / timeout — session probably alive
    ERROR_FATAL     = enum.auto()  # 4xx / unknown — session likely dead for THIS slot

@dataclass(frozen=True)
class CellClickResult:
    slot: AvailableSlot
    outcome: CellOutcome
    latency_ms: int  # for logging only

async def cell_click(self, slot: AvailableSlot) -> CellClickResult:
    """POST make_book.do. Returns synchronously after one round-trip."""

async def submit(self, slot: AvailableSlot) -> BookingResult:
    """POST make_book_submit.do for a slot whose cell_click returned ACCEPTED."""
```

`BookingResult` unchanged (SUCCESS / OCCUPIED / ERROR_TRANSIENT / ERROR_FATAL).

### B1.2 New `book_via_http` orchestrator

```python
async def book_via_http(client, target_date) -> int:
    candidates = _build_candidates(target_date)  # priority-ordered tuple
    _LOG.info("predictive booking: %d candidates queued", len(candidates))

    # Phase 1: parallel cell-clicks
    cell_results = await asyncio.gather(
        *(client.cell_click(s) for s in candidates)
        # return_exceptions=False — see "Error policy" below.
    )

    for rank, (slot, cr) in enumerate(zip(candidates, cell_results)):
        _LOG.info("rank=%d %s: cell=%s (latency=%dms)",
                  rank, slot.facility_name, cr.outcome.name, cr.latency_ms)

    accepted = [(rank, slot) for rank, (slot, cr) in
                enumerate(zip(candidates, cell_results))
                if cr.outcome is CellOutcome.ACCEPTED]

    if not accepted:
        _LOG.warning("no cell_click ACCEPTED; aborting")
        return 1

    # Phase 2: sequential submit, strict priority order
    for rank, slot in accepted:
        br = await client.submit(slot)
        _LOG.info("submit rank=%d %s: %s", rank, slot.facility_name, br.name)
        if br is BookingResult.SUCCESS:
            _LOG.info("done: booked %s @ %s (rank=%d)",
                      slot.facility_name, slot.start_dt.strftime("%H:%M"), rank)
            return 0
        if br is BookingResult.ERROR_FATAL:
            _LOG.error("submit ERROR_FATAL; aborting remaining submits")
            return 1
        # OCCUPIED or ERROR_TRANSIENT — try next-priority ACCEPTED candidate

    _LOG.warning("no candidate succeeded after submit phase")
    return 1
```

Note `return_exceptions=False`: `cell_click` already catches `httpx.HTTPError` and returns
`ERROR_TRANSIENT`. If it raises an unexpected exception, we want the run to crash with a
traceback so CI logs surface it — not be silently swallowed by `gather`.

### B1.3 Warmup expansion

```python
async def warmup(self, n: int = 1) -> list[int]:
    """Open n warm TLS connections in the pool via n concurrent GETs.

    Each httpx GET that overlaps in time forces a new connection because none
    has yet returned to the pool. After all return, the pool holds n hot
    keepalive sockets. The next POST burst (cell_click × n) reuses them all.
    """
    async def _one() -> int:
        try:
            resp = await self._http.get(MAKE_BOOK_URL, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": _REFERER_MAKE_BOOK,
            })
            return resp.status_code
        except httpx.HTTPError:
            return -1
    return await asyncio.gather(*(_one() for _ in range(n)))
```

Call site (`src/booker.py:run`):

```python
candidates = _build_candidates(target_date)
await client.warmup(n=len(candidates))
```

Plus explicit connection-pool limits in `PolyUHttpClient.__init__`:

```python
self._http = httpx.AsyncClient(
    cookies=cookies,
    headers=_DEFAULT_HEADERS,
    timeout=timeout,
    follow_redirects=False,
    limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
)
```

`8` chosen to leave headroom above the current 4 candidates without inviting silly numbers.
Explicit limits make future candidate-set changes audit-friendly.

**Failure policy:** warmup is best-effort. All-`-1` returns are logged but **do not abort**.

## Error policy

### Exit code matrix

| cell_click phase | submit phase | exit | reason |
|---|---|---|---|
| ≥1 ACCEPTED | a submit returns SUCCESS | 0 | main path |
| ≥1 ACCEPTED | all ACCEPTED → OCCUPIED | 1 | lost the race cleanly |
| ≥1 ACCEPTED | a submit returns ERROR_FATAL | 1 | abort remaining submits (auth presumed dead) |
| ≥1 ACCEPTED | ACCEPTED list exhausted via TRANSIENT/OCCUPIED mix | 1 | walked the list, none won |
| 0 ACCEPTED, all OCCUPIED | (skipped) | 1 | slots gone before our cell-clicks landed |
| 0 ACCEPTED, all ERROR | (skipped) | 1 | session/network problem |
| 0 ACCEPTED, mix of OCCUPIED + ERROR | (skipped) | 1 | same |

### Behaviour deltas from current code

- Cell-click ERROR_FATAL **no longer** aborts the run globally. It marks that one candidate
  as un-submittable; the rest proceed normally. This matches the concurrent-attempt mental
  model (a 4xx on facility 10 doesn't mean facility 11 is poisoned).
- Submit ERROR_TRANSIENT **does not** abort the submit loop — advance to the next ACCEPTED
  candidate. (Cell-click already succeeded, so the session is alive; the slot got grabbed.)
- Submit ERROR_FATAL **does** abort the submit loop. If a 4xx hits here, retrying on
  another slot won't help.

### Server-side safety net (informational)

PolyU enforces a 1-booking-per-day quota at the server (`byPassQuota=false`,
`byPassBookingDaysLimit=false`). This means:

- Multiple cell_clicks holding multiple slots is allowed (cell_click reserves a temporary
  hold, no quota charge yet).
- Even if a bug caused us to fire two submits in parallel, PolyU would reject one.
- Our orchestrator's "exit immediately on first SUCCESS" is the user-facing guarantee,
  with the quota as the underlying safety net.

We do not actively rely on the safety net for correctness, but it's why we tolerate a
slightly more aggressive parallel design than would otherwise be prudent.

## Test plan

All tests offline, no network. Per CLAUDE.md "Tests are offline".

### A — classification & diagnostics (`tests/test_http_client.py`)

Use `httpx.MockTransport` with a `PolyUHttpClient` constructed against it.

| test | submit response | expected |
|---|---|---|
| `test_submit_redirect_to_make_book_is_occupied` | `302` + `Location: .../make_book.do` | `OCCUPIED` (regression for 2026-06-07) |
| `test_submit_redirect_to_make_book_submit_is_occupied` | `302` + `Location: .../make_book_submit.do` | `OCCUPIED` (existing behaviour preserved) |
| `test_submit_redirect_to_make_book_result_is_success` | `302` + `Location: .../make_book_result.do` | `SUCCESS` |
| `test_submit_body_occupied_lowercase_is_occupied` | `200` + body contains `"is occupied"` lowercase | `OCCUPIED` |
| `test_submit_body_alt_occupied_wording_is_occupied` | `200` + body contains `"occupied"` in different phrasing | `OCCUPIED` |
| `test_submit_4xx_is_fatal` | `400` + arbitrary body | `ERROR_FATAL` |
| `test_submit_5xx_is_transient` | `503` | `ERROR_TRANSIENT` |
| `test_submit_unknown_shape_is_fatal_with_diagnostics` | `200` + empty Location + body without markers | `ERROR_FATAL`; assert WARNING record contains `body_len=`, `preview=`, `markers=[]` |
| `test_diag_markers_extracts_substrings` | direct unit test of `_diag_markers("...quota exceeded...session expired...")` | returns `['quota', 'exceeded', 'session', 'expired']` |
| `test_cell_click_body_occupied_lowercase_is_occupied` | cell_click `200` + body contains `"occupied"` (any case) | cell outcome `OCCUPIED` (parallel to submit case, A.1 consistency) |

### B1 — orchestrator (`tests/test_http_booker.py`)

Rewrite `_FakeClient` to script per-slot `cell_click` and `submit` outcomes. Track call counts
and per-call slot, plus wall-clock timestamps for the parallelism test.

| test | cell_click outcomes (rank 0..3) | submit script | expected |
|---|---|---|---|
| `test_happy_path_rank0_wins` | all ACCEPTED | rank 0 → SUCCESS | exit 0; exactly 1 submit on rank 0 |
| `test_priority_preserved_when_only_some_accepted` | rank 0,2 ACCEPTED; 1,3 OCCUPIED | rank 0 → SUCCESS | exit 0; submit slot is rank 0 |
| `test_fallback_to_rank1_after_rank0_occupied` | all ACCEPTED | rank 0 → OCCUPIED, rank 1 → SUCCESS | exit 0; 2 submits in order |
| `test_all_occupied_in_cell_phase_exits_1` | all OCCUPIED | (no submit invoked) | exit 1; 0 submits |
| `test_all_accepted_all_submit_occupied_exits_1` | all ACCEPTED | all OCCUPIED | exit 1; 4 submits |
| `test_cell_transient_does_not_block_others` | rank 0,1 ACCEPTED; 2 TRANSIENT; 3 ACCEPTED | rank 0 → SUCCESS | exit 0; 1 submit on rank 0 |
| `test_cell_fatal_does_not_abort_globally` | rank 0 FATAL; rank 1 ACCEPTED | rank 1 → SUCCESS | exit 0; 1 submit on rank 1 |
| `test_submit_fatal_aborts_remaining_submits` | all ACCEPTED | rank 0 → FATAL | exit 1; exactly 1 submit (no rank 1/2/3) |
| `test_submit_transient_continues_to_next` | all ACCEPTED | rank 0 → TRANSIENT, rank 1 → SUCCESS | exit 0; 2 submits |
| `test_all_cell_errors_exits_1` | mix of TRANSIENT + FATAL | (no submit invoked) | exit 1; 0 submits |
| `test_cell_clicks_actually_run_in_parallel` | all ACCEPTED; each `cell_click` sleeps 500ms | rank 0 → SUCCESS | assert total wall-clock < 1.2s (would be 2s+ if serial) |

### Warmup (`tests/test_http_client.py`)

| test | setup | expected |
|---|---|---|
| `test_warmup_fires_n_concurrent_gets` | MockTransport counts GET requests | `warmup(n=4)` triggers exactly 4 GETs |
| `test_warmup_returns_status_codes` | MockTransport returns 200 | `warmup(n=4)` returns `[200, 200, 200, 200]` |
| `test_warmup_swallows_http_errors` | MockTransport raises | `warmup(n=4)` returns `[-1, -1, -1, -1]`, does not raise |
| `test_warmup_default_n_is_1` | | `warmup()` triggers 1 GET (back-compat) |

### Out of scope for tests

- Live PolyU integration (violates offline rule).
- httpx internal connection pooling behaviour (don't test the library).
- Precise asyncio timing assertions tighter than ~200ms (flaky on CI).

## Out of scope / future work

- **Root-cause investigation of the 5–6s first-POST latency.** This design routes around the
  symptom. Improved diagnostics (Section A.2) may reveal whether the latency is PolyU-side
  queueing at the 08:30:00 mark or a client-side issue we missed. Decide after observing
  a week of warmup-N runs in production.
- **Submit-pipelining fallback** ("if rank 0 submit > 2s, fire rank 1 submit speculatively").
  Considered, deferred — quota safety net makes it safe but adds complexity; revisit only if
  data shows rank-0 submits routinely timing out other ACCEPTED slots.
- **Parallel-account sessions** — see `2026-05-20-parallel-booking-sessions-design.md`.
- **Expanded candidate set** (Court 3/4 facility IDs, additional time-of-day slots) — a config
  change orthogonal to this design.

## Files touched

- `src/http_client.py` — split `try_book` into `cell_click` + `submit`; classification fix;
  `_diag_markers`; warmup parameter; connection-pool limits.
- `src/http_booker.py` — new orchestrator using `asyncio.gather`.
- `src/booker.py` — pass `n=len(candidates)` to `warmup`.
- `tests/test_http_client.py` — new classification + diagnostic + warmup tests.
- `tests/test_http_booker.py` — rewrite `_FakeClient`, full orchestrator matrix.

No changes to `src/config.py`, `src/dates.py`, `src/log.py`, or workflow files.
