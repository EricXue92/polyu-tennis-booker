# Incident log

The story behind each rule in `CLAUDE.md`. CLAUDE.md keeps the rule and a
one-clause reason and cites an entry here as `[YYYY-MM-DD]`; this file keeps
the evidence. Newest first. When a run fails in a new way, add an entry here
and at most two lines to CLAUDE.md.

Entry format: **what happened** (with log evidence) → **cause** → **rule**.

---

## 2026-09-20 — every cell_click timed out; both accounts quit after 6s

Run 35476350778, target Sun 2026-09-27, staff + student.

- PolyU was slow around 08:30. Warmup normally finishes in ~1s; staff took
  1.94s and student 3.76s, ending at 08:30:01.806. The trigger awaited every
  account's warmup, so **both** accounts fired 1.8s late.
- All 10 cell_clicks then hit the 6s `ReadTimeout` (no server answer at all).
  The orchestrator had no retry for cell-click ERROR_TRANSIENT, so both
  accounts exited at 08:30:07.8. 19:30 stayed free until the owner booked it
  by hand.
- First occurrence of either symptom across the previous 15 runs.
- Keepalive measurement the same morning (one httpx client, GET, idle, GET):
  connection reused after 3s idle, reconnected after 6s and longer. An
  off-peak cold reconnect cost 100–180ms. So the warmup cannot be moved
  earlier, and the 5.5s "cold handshake" of 2026-06-05 was mostly 08:30
  server load rather than TLS.

**Rules:** cell-click timeouts are retried (`CELL_RETRY_*`: rounds for up to
45s, 15s per request, paced ≥1s, only while nothing is ACCEPTED). Warmup never
delays the trigger (`booker.warm_all`; stragglers keep running;
`max_connections` 8 → 16). `WARMUP_LEAD_SECONDS` stays small. Commit 01d1281.

## 2026-09-19 — uniform sub-second OCCUPIED: courts were pre-reserved

Run 35406016729, target Sat 2026-09-26. All 10 candidates (both accounts,
every hour 17:30–21:30, both courts) got cell=ACCEPTED in ~200ms, then
submit=OCCUPIED within ~0.5s (08:30:00.7–08:30:01.6). The owner confirmed the
courts had been reserved ahead of the public release.

**Rule:** a slot lost to competition answers OCCUPIED after normal submit
latency (~3.8–6s). Sub-second OCCUPIED on _every_ candidate means the slots
were never open to us — nothing to fix. Cell-click ACCEPTED is only session
state and does not imply availability.

## 2026-09-17 — what the ERROR_FATAL submit pages actually say (partly open)

Run 35162642566, target Thu 2026-09-24, first run with visible-text
diagnostics. Rank 0 (18:30 court 1) won. Timeline of the other submits:

| Time         | Rank         | Result                   | Page                                                                                                     |
| ------------ | ------------ | ------------------------ | -------------------------------------------------------------------------------------------------------- |
| 08:30:07.463 | 1 (18:30 c2) | ERROR_FATAL, 29640 bytes | visible text around markers is only nav chrome; the `quota` marker hit is in non-visible HTML            |
| 08:30:08.23  | 2, 3 (19:30) | ERROR_FATAL, 29715 bytes | `24 Sep 2026: 2.0 hour(s) of personal booking in a day [Maximum 1 hours per day. Reset quota at 00:00.]` |
| 08:30:08.988 | 0 (18:30 c1) | SUCCESS                  | —                                                                                                        |
| 08:30:10.8   | 4, 5 (20:30) | OCCUPIED, 29784 bytes    | —                                                                                                        |

- **Settled:** the 29715-byte page is the quota page. It reached the 19:30
  submits 0.7s _before_ rank 0's SUCCESS response arrived, so PolyU's quota
  check counts a sibling commit that is still in flight ("2.0 hours" = the
  pending 18:30 hour + this one). That also explains 2026-09-07, when the
  "quota" page showed up before any booking existed.
- **Still open:** the 29640-byte page is _not_ that page — 75 bytes shorter
  and no quota sentence in its visible text. Rank 1 got it before rank 0 had
  succeeded; on 2026-09-07 both 18:30 submits got it and the 19:30 group then
  booked. Its message sits outside every `_DIAG_MARKERS` window and beyond the
  300-char preview (which is all navigation). To settle it, log the visible
  text after the `Make Booking` breadcrumb on ERROR_FATAL.

## 2026-09-16 — Wednesday targets 0/4; third weekday rung added

Wednesday targets went 0/4: every submit came back OCCUPIED ~3s after 08:30
(normal latency, i.e. genuinely contested). Weekday evenings
are contested, so `SLOT_PRIORITY` gained a 20:30 rung as cheap insurance
(18:30 → 19:30 → 20:30). Commit 63664c5.

## 2026-09-06 / 09-09 — raw-HTML preview never reached the error message

`submit unexpected` logged `body[:300]`, which for PolyU's ~30 KB error pages
is `<!DOCTYPE html> <html> <head> ...` — useless. The page assumed to be the
quota page could not be confirmed from logs.

**Rule:** `preview` and `context` in cell_click/submit diagnostics are built
from the page's visible text (`_visible_text`, `_diag_context`), never raw
HTML. Commit 63664c5. (Follow-up findings: 2026-09-17.)

## 2026-08-29 — PolyU's quota, not our code, prevents double-booking

Rank 1's submit got the quota page after rank 0 had won. Quota is one booking
per day, so a surplus commit is rejected server-side.

**Rule:** no auto-cancel path. `book_via_http` still logs a multi-SUCCESS
WARNING listing surplus bookings for manual cancel, in case the quota rule
ever changes. A FATAL alongside a SUCCESS in another group is this rejection —
expected, not an error.

## 2026-08-19 .. 2026-08-27 — 7 lost runs: serial groups + one shared timeout

Every day both 18:30 submits hung past the single shared 6s client timeout
and died on `ReadTimeout` without returning SUCCESS or OCCUPIED. Groups ran
strictly one after the other, so the 19:30 fallback fired only _after_ that
hang and was always OCCUPIED by then. The one win in the window answered at
3.79s. None of the 7 timed-out days produced a court, confirming that
aborting a submit rolls the booking back server-side.

**Rules:**

- Two timeout budgets: `timeout=6.0` for cell_click/warmup, `submit_timeout=20.0`
  for `make_book_submit.do` (legitimately 4–6s+ at 08:30; 6s sat in the middle
  of that distribution). Waiting on a submit is strictly better than aborting.
- Submit groups are staggered, never serialized (`SUBMIT_STAGGER_SECONDS =
2.5`): 18:30 still reaches PolyU's queue first (~2.3s earlier), and 19:30
  always gets a live shot. On a healthy day 18:30 answers at ~3.8s, after the
  stagger, so the 19:30 submits do fire and get quota-rejected — expected
  noise.
- Winner is chosen by rank across all groups, never by arrival order.

Spec: `docs/superpowers/specs/2026-08-27-submit-timeout-and-staggered-groups-design.md`.
Commit 30455f4.

## 2026-06-18 — one hung rank-0 submit locked out its siblings

Submits ran serially by rank. A slow rank-0 submit (10s `ReadTimeout`) locked
out the whole chain behind it.

**Rule:** submits within a time-slot group fire concurrently; strict
court-priority inside a group is deliberately sacrificed.

## 2026-06-09 — 200 with an empty Location

A response shape no classifier branch matched, and not reproducible after the
fact.

**Rule:** every `ERROR_*` from cell_click/submit logs status + Location +
body_len + preview + markers + context, so anomalies are root-causeable from
CI logs alone.

## 2026-06-07 — `302 → make_book.do` rebound misclassified as FATAL

The OCCUPIED detector matched only one narrow response shape.

**Rule:** OCCUPIED = "occupied" (case-insensitive) in the body **or** any
`make_book*` redirect. Spec: `2026-06-10-parallel-cell-click-design.md`.

## 2026-06-05 — first POST at 08:30 took 5.5s on a cold connection

The pool had gone idle during the ~50s between bootstrap and the trigger.

**Rule:** warm one connection per candidate ~2s before the trigger; a single
warm connection only helps the first concurrent POST. (See 2026-09-20 for the
keepalive measurement and the limits of this rule.)

## 2026-06-03 — Search is too slow for the hot path

Measured during the HTTP-replay work: PolyU's Search endpoint takes ~4.5s server-side, long enough to lose every
desired slot. `book_via_http` fabricates `AvailableSlot`s from the candidate
set and posts straight to `make_book.do`. Plan:
`docs/superpowers/plans/2026-06-03-http-integration-phase-2b.md`.

## 2026-05-10 — GitHub's scheduled cron was unreliable

GH Actions' `schedule:` trigger proved unreliable, and runner queue delays of
up to 35 minutes were observed. A Cloudflare Worker now dispatches the workflow
at 07:30 HKT (60-min lead; the booker sleeps to 08:30 regardless) and a second
cron at 08:35 opens a GitHub issue if the day has no successful run. Spec:
`2026-05-10-cloudflare-trigger-design.md`.
