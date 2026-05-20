# Parallel Booking Sessions

**Date:** 2026-05-20
**Status:** Approved, pending implementation plan

## Problem

The booker reliably hits 08:30:00.000 HKT but still loses popular slots to human users in the click-through race. Today's run (booking 2026-05-27) is representative:

```
08:30:00.000  Search clicked
08:30:02.412  clicked 19:30 cell (still available)
08:30:05.743  clicked Submit
08:30:07.086  "Facility occupied" — lost to a human in ~3s race window
08:30:10      retry 18:30 → already gone
08:30:13      retry 20:30 → already gone
08:30:15      retry 17:30 → already gone  → exit 1
```

The 5-second click-through (cell → Next → checkbox → Submit) per attempt, combined with sequential retries, means by the time we've tried our second-choice slot it has also been taken. Single-session retry cannot win this race because each retry takes ~3s of restart overhead plus another 5s of clicks, while peak slots are claimed within 1–3s of release.

## Goal

Win the click-through race by attempting every priority slot **simultaneously** instead of sequentially, while preserving:

- 08:30:00.000 trigger alignment (the hard PolyU constraint).
- No double-booking under any concurrency scenario.
- All existing offline unit-test guarantees.
- The CF Worker → GitHub Actions trigger chain (no infra changes).

## Non-goals

- Reverse-engineering PolyU's HTTP booking form (deferred Approach B). The Playwright click path is kept for resilience to UI changes.
- Booking multiple slots per day. PolyU enforces (or appears to enforce) one booking per user per day; we exploit that for the safety guarantee but do not depend on it.
- Improving login speed. Login is already pre-08:30 and not on the critical path.

## SLOT_PRIORITY change

Drop the 20:30–21:30 candidate. New priority list:

```python
SLOT_PRIORITY = (
    (time(19, 30), time(20, 30)),
    (time(18, 30), time(19, 30)),
    (time(17, 30), time(18, 30)),
)
```

This makes parallel-session count `N = 3` on Mon/Wed/Thu/Fri/Sat/Sun and `N = 1` on Tuesdays (where staff-reserved filtering removes 18:30 and 19:30, leaving only 17:30). Tuesday runs degrade to today's single-session behavior automatically.

## Architecture

Replace the current linear `booker.run()` flow with **N parallel Playwright `BrowserContext` instances** coordinated through asyncio. Each session is a self-contained login + booking flow against the same PolyU account, pre-assigned to one priority slot.

```
T-60s (08:29:00):
  ├── session[0] (19:30) ── login ── prepare_search ── sleep
  ├── session[1] (18:30) ── login ── prepare_search ── sleep
  └── session[2] (17:30) ── login ── prepare_search ── sleep

T0 (08:30:00.000):
  all sessions, in parallel:
    Search → click target cell → Next → tick agreement
  ↓ converge at "Submit-ready"

Serialized phase (in priority order 0 → 1 → 2):
  session[0] acquires submit_lock → click Submit → fast-fail race
    success → set win_event → DONE (exit 0)
    "occupied" → release lock
  session[1] acquires lock → click Submit → ...
  session[2] ...
  all-fail → exit 1
```

The Submit step is **serialized behind an `asyncio.Lock`**, attempted in priority order. Everything before Submit runs in parallel. This guarantees no double-booking even if PolyU permits it.

## Components

### New: `src/parallel_runner.py`

Orchestrator. Owns the asyncio event loop, spawns N sessions, holds the win-event and the Submit-lock. Top-level entry point replacing the current `book-tennis` flow's `run()` body.

Exposed API:

```python
async def run_parallel(slots: Sequence[SlotWindow], target_date: date) -> int:
    """Returns process exit code: 0 if any session books, 1 otherwise."""
```

Internals:

- `win_event = asyncio.Event()` — set by the first session that lands a successful Submit. All other sessions check it before acting and bail out if set.
- `ready_queue: asyncio.PriorityQueue[(rank, asyncio.Event)]` — when a session reaches Submit-ready state, it creates a personal `proceed_event`, enqueues `(rank, proceed_event)`, and awaits the event. A single coordinator task pulls in priority order, sets the dequeued session's `proceed_event`, awaits the session's `submit_and_resolve` outcome, then pulls the next. This **single-dequeuer** pattern is the entire serialization mechanism — no separate `asyncio.Lock` is needed, because only one session is ever signaled to Submit at a time.

### Refactor: `src/booker.py`

Split the existing `run()` into per-session phases callable from `parallel_runner`:

```python
async def login_and_prepare(ctx, slot, target_date) -> None: ...
async def search_and_click_through(ctx, slot, target_date) -> None: ...
async def submit_and_resolve(ctx, slot) -> BookingResult: ...
```

`BookingResult` is an enum: `SUCCESS`, `OCCUPIED`, `ERROR`. The existing fast-fail race against the "Facility occupied" banner moves into `submit_and_resolve`. The retry logic in today's `run()` (`restart_to_results` on `BookingFailed`) is removed — parallel sessions replace it.

`book-tennis` CLI entrypoint switches to calling `parallel_runner.run_parallel(...)`.

### Refactor: `src/log.py`

Extend `build_logger` to accept an optional `session_id` and prepend `[s0] ` / `[s1] ` / `[s2] ` to each record. The existing password-redaction filter stays unchanged. With 3 sessions logging concurrently, line interleaving needs the prefix to remain grep-able.

### Config: `src/config.py`

- Remove `(time(20, 30), time(21, 30))` from `SLOT_PRIORITY`.
- No new flags. `N` is derived from `len(slot_priority_for(target_date))`.

### Artifacts

Each session writes namespaced screenshots: `pre_submit_s0.png`, `post_submit_s0.png`, `failure_s0.png`, etc. `book.yml` already uploads the whole `artifacts/` directory, so no workflow change needed.

## Data flow

```
parallel_runner.run_parallel(slots, target_date)
  ├─ playwright.chromium.launch (one browser, N contexts)
  ├─ asyncio.gather(
  │     session(0, slots[0], rank=0),
  │     session(1, slots[1], rank=1),
  │     session(2, slots[2], rank=2),
  │  )
  │     each session:
  │       ├─ login_and_prepare(slot)          [parallel, pre-08:30]
  │       ├─ wait_until_trigger_time()
  │       ├─ if win_event.is_set(): return
  │       ├─ search_and_click_through(slot)   [parallel, post-08:30]
  │       ├─ if win_event.is_set(): return
  │       ├─ ready_queue.put(rank)
  │       └─ await proceed_event              [serialized by coordinator]
  │             ├─ if win_event.is_set(): return
  │             ├─ submit_and_resolve()
  │             │     SUCCESS  → win_event.set()
  │             │     OCCUPIED → log, exit session
  │             │     ERROR    → log, exit session
  │             └─ coordinator pulls next from ready_queue
  └─ return 0 if win_event.is_set() else 1
```

The "own_submit_turn" mechanism: a single coordinator task pulls from `ready_queue` in priority order and signals the matching session to proceed. Sessions await their personal signal. This is more robust than each session grabbing the lock directly because it prevents a fast low-priority session from cutting in front of a slow high-priority one.

## Error handling

- **Login failure** (`LoginFailed`) in any single session → log, that session exits, other sessions continue. If all three fail login → exit 1.
- **Playwright timeout** in pre-Submit phase → that session exits, others continue.
- **Concurrent same-account session rejection by PolyU** (unknown until tested) → would surface as Playwright timeouts or `LoginFailed` in some sessions. If consistent, design falls back to N=1 (single session) and we revisit with Approach B (HTTP fast-path).
- **`win_event` set mid-flow** → every session checks at well-defined yield points (after `login_and_prepare`, after `search_and_click_through`, immediately after acquiring submit-turn) and bails cleanly, closing its context.

## Testing

Existing offline tests stay; add new ones:

- `tests/test_parallel_runner.py` — use a `FakeSession` (an async stub with configurable success/failure/delay) to verify:
  - First success sets `win_event` and stops others.
  - Submits happen in priority order even if higher-priority sessions are slower to reach Submit-ready.
  - If priority-0 fails with OCCUPIED, priority-1 gets the Submit lock next.
  - All-failure path returns exit code 1.
  - `win_event` bail-out at each yield point.
- `tests/test_log.py` — verify `session_id` prefix is applied and password redaction still works.
- No live integration tests (per existing convention). Validation is via `--dry-run` against the real site.

Validation plan before promoting to production:

1. **Smoke test with N=2** — temporarily set `SLOT_PRIORITY` to two slots, run `--dry-run --skip-sleep` locally, confirm both sessions complete login and Search without one invalidating the other. If broken, escalate to Approach B.
2. **Live N=3 dry-run** — run `gh workflow run "Daily Tennis Booking" -f dry_run=true` before 08:30 HKT so candidate slots are still available; verify three `pre_submit_s{0,1,2}.png` artifacts.
3. **Live N=3 real booking** — let the scheduled CF-triggered run execute. Watch for exit 0 and a single successful booking.

## What stays the same

- CF Worker triggering at 07:30 HKT.
- 08:30:00.000 sleep alignment via `sleep_until_hkt`.
- Pre-login timing (`PRELOGIN_LEAD_SECONDS = 60`).
- `Selectors` dataclass, weekday exclusions, fast-fail Submit race.
- Exit codes: 0 = booked, 1 = not booked. Notification path unchanged.
- Offline-only test posture.

## Risks & open questions

| Risk | Mitigation |
|---|---|
| PolyU rejects/invalidates concurrent same-account sessions | Smoke test with N=2 first. If broken → fall back to Approach B (HTTP POST). |
| Free-tier GitHub runner can't fit 3 Chromium contexts (memory) | Measure during dry-run. If tight, reuse a single browser with 3 contexts (lighter than 3 browsers). |
| Race condition between `win_event.set()` and another session being signaled to Submit | Single-dequeuer coordinator + `win_event` check after `proceed_event` fires closes this window. |
| Log interleaving makes debugging harder | `[sN]` prefix on every line; per-session artifact namespacing. |
| Tuesday degrades to single session (no parallel benefit) | Accepted. The booker still works; no change vs. today's behavior. |

## Out of scope (future work)

- **Approach B (HTTP POST fast path)** — if PolyU breaks concurrent sessions or the parallel approach still loses peak slots consistently, revisit. Sub-second Submit would make even sequential retries fast enough.
- **Multiple PolyU accounts** — would double the win rate but requires user-supplied credentials and is a separate design.
- **Off-peak slot expansion** — orthogonal change to SLOT_PRIORITY; not blocked by this design.
