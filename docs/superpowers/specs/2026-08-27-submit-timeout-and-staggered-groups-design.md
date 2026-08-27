# Submit timeout split + staggered submit groups — design

**Date:** 2026-08-27
**Status:** Implemented.
**Supersedes** the "HTTP client timeout is 6s" and strictly-serial-group
invariants from `2026-06-10-parallel-cell-click-design.md`.

## Context

Last 10 dispatched runs (2026-08-17 → 2026-08-27). Two were Tuesday rest-day
skips (exit 0, not real attempts). Of the 8 real runs: **1 success, 7 failures.**

| Target date | 18:30 group (rank 0/1) | 19:30 group (rank 2/3)             | Result     |
| ----------- | ---------------------- | ---------------------------------- | ---------- |
| 09-03 Thu   | ReadTimeout @6.0s ×2   | OCCUPIED @1.2s                     | fail       |
| 09-02 Wed   | ReadTimeout @6.0s ×2   | OCCUPIED @5.9s                     | fail       |
| 08-31 Mon   | ReadTimeout @6.0s ×2   | 500 maintenance page + ReadTimeout | fail       |
| 08-30 Sun   | ReadTimeout @6.0s ×2   | OCCUPIED @4.7s                     | fail       |
| 08-29 Sat   | **SUCCESS @3.79s**     | not reached                        | **booked** |
| 08-28 Fri   | ReadTimeout @6.0s ×2   | ReadTimeout ×2                     | fail       |
| 08-27 Thu   | ReadTimeout @6.0s ×2   | OCCUPIED @1.25s                    | fail       |
| 08-26 Wed   | ReadTimeout @6.0s ×2   | OCCUPIED @1.8s                     | fail       |

Everything upstream of `submit` was healthy on every one of those days: login
and `bootstrap_http_client` completed by 08:29:09, warmup opened 4 connections
(all 200), the trigger fired at 08:30:00.001, and all four `cell_click`s
returned ACCEPTED in 150–205ms. The slots were held server-side. Only
`make_book_submit.do` failed.

## Root cause

**We were cutting off our own submits at 6.0s.**

The 18:30 submits never returned OCCUPIED and never returned SUCCESS — they
hung until `httpx` raised `ReadTimeout` at the client-side budget. The single
win in the window answered at 3.79s, and the historical slowest legitimate
SUCCESS was 5.3s. `PolyUHttpClient(timeout=6.0)` therefore sat in the middle of
the submit latency distribution rather than above it — exactly the condition
the 2026-06 design flagged as "reopen if a legitimate submit ever needs >6s".

The user confirmed no court was ever silently booked on a timed-out day, so
aborting the request rolls the transaction back server-side. Waiting is
strictly better than giving up.

**Compounding factor: strictly serial groups.** `book_via_http` awaited each
time-slot group to completion before starting the next. So the 18:30 hang spent
the entire 6s budget before the 19:30 fallback was even dispatched — and by
t=6s the 19:30 slot was already gone (its OCCUPIED answers came back in
1.2–1.8s, i.e. the slot had been taken well before we asked). Simply raising
the timeout in place would have made this _worse_: a 20s budget would push the
fallback to t=20s.

## Decision

Two coupled changes; neither works without the other.

1. **Split the timeout budget.** `PolyUHttpClient(timeout=6.0,
submit_timeout=20.0)`. `timeout` still guards `cell_click` and `warmup` —
   they run 150–300ms on the warm pool and are all `gather`ed together, so one
   hang stalls the whole submit phase behind it. `submit` gets its own 20s read
   budget via a per-request `httpx.Timeout`, with **connect left on the short
   budget** (the pool is warm at 08:30, so a slow handshake means something is
   wrong, not that we are queued).

2. **Stagger the groups instead of serializing them.** Group N+1 launches when
   the earlier groups settle _or_ `SUBMIT_STAGGER_SECONDS` (2.5s) elapses,
   whichever comes first — `asyncio.wait(launched, timeout=stagger_s)`. All
   groups are then awaited together and the winner is the **lowest-rank**
   SUCCESS, not the first to answer.

`SUBMIT_STAGGER_SECONDS = 2.5` is sized so it does not pre-empt a run that is
simply working (healthy submits answer in 3.8–5.3s, and 18:30 stays ~2.3s ahead
of 19:30 in PolyU's queue) while a hung group no longer consumes the fallback's
window.

## Consequences

- On a healthy day the 18:30 submit answers _after_ the stagger, so the 19:30
  submits now fire too and are quota-rejected by PolyU. That ERROR_FATAL
  WARNING is expected noise, not a regression.
- Double-booking is not a new risk: PolyU's quota permits one booking per day,
  observed directly on 2026-08-29 when rank 1 received the quota page after
  rank 0 won. The multi-SUCCESS WARNING is retained as a belt-and-braces check
  in case that rule changes.
- Results no longer arrive in priority order, so rank-based winner selection is
  now load-bearing rather than incidental. `test_slow_first_group_success_
beats_fast_second_group_success` locks it.
- A run that loses every candidate now takes ~20s instead of ~12s. Irrelevant —
  the job has no deadline after 08:30.

## Rejected alternatives

- **Raise the shared timeout to 15s, keep serial groups.** Rescues 18:30 but
  abandons the 19:30 fallback entirely (it would fire at t=15s, long gone).
- **Fire all submits fully in parallel at t=0.** Safe under a 1-booking/day
  quota, but gives up the priority signal: whichever slot PolyU commits first
  wins, and 19:30 could beat the preferred 18:30.

## Verification

- `uv run pytest` — 94 passed. New coverage:
  `test_submit_uses_longer_read_timeout_than_default`,
  `test_cell_click_keeps_the_short_default_timeout`,
  `test_hung_group_does_not_delay_next_group_past_stagger`,
  `test_slow_first_group_success_beats_fast_second_group_success`,
  `test_success_in_later_group_survives_fatal_in_earlier_group`.
- Replaying the observed 09-03 timings through the new orchestrator (18:30
  answering SUCCESS at 8s, 19:30 OCCUPIED at 1.2s) returns rc=0 with rank 0 as
  the winner; the old code returned rc=1.
- Not verified live. The next contested morning is the real test — check
  whether the 18:30 submit now returns a verdict instead of a ReadTimeout, and
  what its true latency is.
