"""HTTP-based booking orchestrator (parallel cell-clicks, time-grouped submits).

Phase 1: fire all (priority x facility) cell_click POSTs concurrently via
asyncio.gather. They finish in roughly first-POST latency, not stacked, so
the "first-POST 5-6s cold tax" no longer cascades onto candidates 2-4.

Phase 2: group ACCEPTED cells by their (start, end) time-slot, preserving rank
order. Within each group, fire submits concurrently via asyncio.gather. Groups
are *staggered*, not serialized: group N+1 launches as soon as the earlier
groups settle OR SUBMIT_STAGGER_SECONDS elapses, whichever comes first. Once
every group has been launched we await them all and pick the lowest-rank
SUCCESS.

Why staggered rather than serial. Groups used to run strictly one after the
other, so a hung high-priority submit spent its entire timeout budget before
the fallback group even started. That cost 7 consecutive runs
(2026-08-19..2026-08-27): both 18:30 submits hung to the client timeout every
day, and the 19:30 fallback, only fired afterwards, came back OCCUPIED every
time. The stagger keeps 18:30 first in PolyU's queue (it is dispatched
~2.5s earlier and arrives within ~200ms of 08:30:00) while guaranteeing the
19:30 fallback still gets a live shot at its window.

Priority semantics: the user's preference is time-major, facility-minor
("any court at 18:30 before 19:30"). Overlapping groups mean results no longer
arrive in priority order, so the winner is chosen by rank across all results
rather than by whichever group answers first. Strict facility-priority within
a time-slot is still sacrificed - a slow rank 0 submit (10s ReadTimeout on
2026-06-18) used to lock out the whole chain.

Double-booking is not a risk here: PolyU's quota permits one booking per day,
so a second commit is rejected server-side with the quota page (observed
2026-08-29, where rank 1 returned it after rank 0 had won). The multi-SUCCESS
WARNING below is retained as a belt-and-braces check in case that quota rule
ever changes.

Cell-click ERROR_TRANSIENT and ERROR_FATAL are tolerated at the candidate
level - a facility-specific failure does not poison sibling candidates.

Cell-click retry. When a round ends with nothing ACCEPTED but some candidates
ERROR_TRANSIENT, those candidates are re-fired (see CELL_RETRY_* below) until
one is ACCEPTED, none is transient any more, or the window closes. On
2026-09-20 PolyU was slow at 08:30, all 10 cell_clicks (both accounts) died on
the 6s ReadTimeout, and the run quit at 08:30:07 - while 19:30 stayed free
long enough for the owner to book it by hand. A timeout says nothing about
the slot, so giving up on it is never right.

403 on a cell-click is retried too, after re-reading the CSRFToken, at most
CELL_403_REFRESH_MAX times: on 2026-11-14 the student site answered every
re-click with its 403 "There is a problem" page while the session was still
alive, and the run quit at 08:30:13 without a single submit.

Submit retry. A submit answered 403 (ERROR_FORBIDDEN), 5xx or a transport
error says nothing about the slot either. That candidate is re-clicked and
re-submitted - after a CSRFToken refresh for a 403 - up to SUBMIT_RETRY_MAX
times while SUBMIT_RETRY_WINDOW_SECONDS is open and no submit of the account
has come back SUCCESS or ERROR_FATAL. A 403 no longer stops later groups from
launching. On 2026-11-21 PolyU's backend fell over at 08:30: staff's submits
answered 500 "We'll be back soon!" and 403 after 10-15s and were dropped, and
student's first group answered 403 in 0.37s, which (403 being ERROR_FATAL
then) kept its other two hours from ever being submitted. The first submit of
every candidate is sent exactly as before; the retry only runs after PolyU has
answered it without a verdict.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Awaitable, Callable, Protocol

from src.http_client import (
    AvailableSlot,
    BookingResult,
    CellClickResult,
    CellOutcome,
)


# How long a group gets to answer before the next-priority group is launched
# alongside it. Sized against production timings: a healthy submit answers in
# ~3.8-5.3s, so 2.5s does not pre-empt a run that is simply working, while a
# hung group (>6s, the 2026-08 failure mode) no longer consumes the fallback's
# window. Lower it to favour the fallback, raise it to favour strict priority.
SUBMIT_STAGGER_SECONDS = 2.5

# Cell-click retry (only when a round produced no ACCEPTED at all).
# WINDOW: how long after the first round new retry rounds may still start.
# TIMEOUT: per-request budget for retries. The first round keeps the client's
#   short default because a hung cell_click stalls ready submits behind it;
#   a retry round has nothing ready to stall, and on a day when PolyU needs
#   >6s per request a second 6s attempt would just time out again.
# MIN_ROUND: floor on a round's duration so instant failures (connection
#   refused, 503) are paced instead of hammering the server.
CELL_RETRY_WINDOW_SECONDS = 45.0
CELL_RETRY_TIMEOUT_SECONDS = 15.0
CELL_RETRY_MIN_ROUND_SECONDS = 1.0
# How many retry rounds may re-click candidates that answered 403, each after
# a CSRFToken refresh. Capped because a 403 answers in ~150ms: a session that
# is really dead must not be hammered for the whole window.
CELL_403_REFRESH_MAX = 3

# Submit retry. A submit answered 403, 5xx or a transport error carries no
# verdict on the slot, so the candidate is re-clicked and re-submitted (after
# a CSRFToken refresh for a 403). Never touches the first submit of any
# candidate - those still leave at the trigger, untouched.
# WINDOW: measured from the first submit; no retry starts after it. Sized so a
#   submit that queued 10-15s before failing (2026-11-21) still gets one more
#   go, while a 45s ReadTimeout is left alone - by then the slot is long gone
#   and the booking may have gone through anyway [2026-10-16].
# MAX: retries per candidate.
# GAP: minimum spacing between one candidate's attempts; a 403 answers in
#   ~150ms and a dead session must not be hammered.
# 403_REFRESH_MAX: token refreshes per account during the submit phase.
SUBMIT_RETRY_WINDOW_SECONDS = 30.0
SUBMIT_RETRY_MAX = 2
SUBMIT_RETRY_GAP_SECONDS = 1.0
SUBMIT_403_REFRESH_MAX = 2
_SUBMIT_RETRYABLE = (BookingResult.ERROR_FORBIDDEN, BookingResult.ERROR_TRANSIENT)
# Answers that end retrying for the whole account: booked, or the non-403
# error page that has meant "booked" before - the records decide that one.
_SUBMIT_FINAL = (BookingResult.SUCCESS, BookingResult.ERROR_FATAL)


# Awaited once between the cell-click round and the first submit; the
# production implementation sleeps until TRIGGER_TIME_HKT.
TriggerWait = Callable[[], Awaitable[None]]


# Note carried into the result email when the booking was found in PolyU's
# records rather than confirmed by the submit response itself.
RECORDS_NOTE = "提交响应异常，已按 PolyU 预订记录确认"


class _ClientLike(Protocol):
    """Minimal subset of PolyUHttpClient used by the orchestrator."""
    async def cell_click(
        self, slot: AvailableSlot, *, timeout: float | None = None,
    ) -> CellClickResult: ...
    async def submit(self, slot: AvailableSlot) -> BookingResult: ...
    async def refresh_csrf_token(
        self, *, timeout: float | None = None,
    ) -> bool | None: ...
    async def booking_records(self) -> list[AvailableSlot] | None: ...


def _build_candidates(
    target_date: date,
    slots: list[tuple[time, time]],
) -> list[AvailableSlot]:
    from src.config import TENNIS_CENTER_NAME, TENNIS_CTR_ID, TENNIS_FACILITIES

    candidates: list[AvailableSlot] = []
    for start, end in slots:
        start_dt = datetime.combine(target_date, start)
        end_dt = datetime.combine(target_date, end)
        for fid, fname in TENNIS_FACILITIES.items():
            candidates.append(AvailableSlot(
                facility_id=fid,
                facility_name=fname,
                center_id=TENNIS_CTR_ID,
                center_name=TENNIS_CENTER_NAME,
                start_dt=start_dt,
                end_dt=end_dt,
            ))
    return candidates


@dataclass(frozen=True)
class BookingOutcome:
    """Exit code plus the winning slot (None on failure and on a dry run).
    `note` is set when the slot came from PolyU's booking records rather
    than from a SUCCESS response (see `_recorded_booking`)."""
    rc: int
    slot: AvailableSlot | None = None
    note: str = ""


async def _recorded_booking(
    client: _ClientLike,
    target_date: date,
    log: logging.Logger,
) -> AvailableSlot | None | bool:
    """What PolyU's own records say about target_date, fetched after the
    submit phase (off the hot path).

    Returns the recorded tennis slot on target_date, None when the records
    were read and hold nothing for that date, or False when the page could
    not be fetched (unknown - keep the submit verdict).

    Why this exists: since PolyU started queueing submits (2026-10) the
    response no longer tells the truth. A booking that went through has
    answered with a ReadTimeout (10-12) and with the 42689-byte "Failed to
    reserve facility" page classified ERROR_FATAL (10-14, 10-15, 10-16),
    so the owner was emailed "not booked" for courts that were booked.
    """
    records = await client.booking_records()
    if records is None:
        log.warning("booking records unavailable; keeping the submit verdict")
        return False
    log.info(
        "booking records: %s",
        [f"{s.start_dt:%Y-%m-%d %H:%M} {s.facility_name}" for s in records] or "none",
    )
    for slot in records:
        if slot.start_dt.date() == target_date:
            return slot
    return None


async def book_via_http(
    client: _ClientLike,
    target_date: date,
    slots: list[tuple[time, time]],
    dry_run: bool,
    *,
    log: logging.Logger,
    stagger_s: float = SUBMIT_STAGGER_SECONDS,
    cell_retry_window_s: float = CELL_RETRY_WINDOW_SECONDS,
    cell_retry_min_round_s: float = CELL_RETRY_MIN_ROUND_SECONDS,
    submit_retry_window_s: float = SUBMIT_RETRY_WINDOW_SECONDS,
    submit_retry_gap_s: float = SUBMIT_RETRY_GAP_SECONDS,
    wait_for_trigger: TriggerWait | None = None,
) -> int:
    """Run the booking flow. Returns 0 on SUCCESS, 1 otherwise."""
    outcome = await book_via_http_outcome(
        client, target_date, slots, dry_run, log=log, stagger_s=stagger_s,
        cell_retry_window_s=cell_retry_window_s,
        cell_retry_min_round_s=cell_retry_min_round_s,
        submit_retry_window_s=submit_retry_window_s,
        submit_retry_gap_s=submit_retry_gap_s,
        wait_for_trigger=wait_for_trigger,
    )
    return outcome.rc


async def book_via_http_outcome(
    client: _ClientLike,
    target_date: date,
    slots: list[tuple[time, time]],
    dry_run: bool,
    *,
    log: logging.Logger,
    stagger_s: float = SUBMIT_STAGGER_SECONDS,
    cell_retry_window_s: float = CELL_RETRY_WINDOW_SECONDS,
    cell_retry_min_round_s: float = CELL_RETRY_MIN_ROUND_SECONDS,
    submit_retry_window_s: float = SUBMIT_RETRY_WINDOW_SECONDS,
    submit_retry_gap_s: float = SUBMIT_RETRY_GAP_SECONDS,
    wait_for_trigger: TriggerWait | None = None,
) -> BookingOutcome:
    """Run the parallel cell-click + staggered time-grouped submit flow.

    Same flow as `book_via_http`, but also reports which slot won.

    `wait_for_trigger`, when given, is awaited between the cell-click round
    and the first submit (production: sleep until 08:30:00.000). The caller
    then starts this coroutine *before* slot-open so the cell-click round -
    one full round trip, ~0.3s from a GitHub runner - is already paid when
    the trigger fires and the submits leave at 08:30:00.000 sharp. A
    cell-click only records the chosen cell in the server session; it does
    not check availability [2026-09-19], which is what makes clicking early
    plausible. If PolyU refuses the pre-open click (no candidate ACCEPTED),
    the round is repeated after the trigger, i.e. the pre-2026-10-16 flow.
    """
    candidates = _build_candidates(target_date, slots)
    log.info("predictive booking: %d candidates queued", len(candidates))

    if dry_run:
        log.info("DRY RUN: stopping before cell_click phase")
        await _recorded_booking(client, target_date, log)
        return BookingOutcome(0)

    def _log_round(results: list[CellClickResult]) -> list[CellClickResult]:
        for rank, cr in enumerate(results):
            log.info(
                "rank=%d %s: cell=%s (latency=%dms)",
                rank, cr.slot.facility_name, cr.outcome.name, cr.latency_ms,
            )
        return results

    async def _click_round() -> list[CellClickResult]:
        # Parallel cell-clicks.
        # return_exceptions=False - cell_click catches httpx.HTTPError
        # internally and returns ERROR_TRANSIENT. Any uncaught exception is a
        # real bug and should crash the run with a traceback in CI, not be
        # silently swallowed.
        return _log_round(list(await asyncio.gather(
            *(client.cell_click(slot) for slot in candidates)
        )))

    async def _preclick_round(trigger: TriggerWait) -> list[CellClickResult]:
        """Fire the round, then wait for the trigger - never for the round.

        The round used to be gathered before the trigger was awaited, so one
        hung click held every submit: on 2026-11-05/06/09 three of ten
        pre-clicks ran into the 6s ReadTimeout and the submits left at
        08:30:06+. Now whatever is ACCEPTED when the trigger fires is
        submitted, and clicks still in flight are dropped (reported as
        ERROR_TRANSIENT). Only when nothing is ACCEPTED yet is the round
        awaited to its end - nothing is ready to submit, so that costs nothing.
        """
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        tasks = [asyncio.ensure_future(client.cell_click(slot)) for slot in candidates]
        try:
            # Let every click start (and put its request on the wire) before
            # the trigger coroutine gets to run.
            await asyncio.sleep(0)
            await trigger()
            if not any(
                t.done() and not t.cancelled() and t.exception() is None
                and t.result().outcome is CellOutcome.ACCEPTED
                for t in tasks
            ):
                await asyncio.wait(tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            raise
        results: list[CellClickResult] = []
        dropped = 0
        for slot, t in zip(candidates, tasks):
            if t.done():
                results.append(t.result())  # re-raises a real bug
            else:
                t.cancel()
                dropped += 1
                results.append(CellClickResult(
                    slot=slot, outcome=CellOutcome.ERROR_TRANSIENT,
                    latency_ms=int((loop.time() - t0) * 1000),
                ))
        if dropped:
            log.warning(
                "pre-click: %d cell_click(s) still in flight at the trigger; "
                "submitting without them", dropped,
            )
        return _log_round(results)

    # Phase 1: cell-click round (pre-click when a trigger is supplied).
    if wait_for_trigger is None:
        cell_results = await _click_round()
    else:
        cell_results = await _preclick_round(wait_for_trigger)
        n_accepted = sum(cr.outcome is CellOutcome.ACCEPTED for cr in cell_results)
        if n_accepted:
            log.info(
                "pre-click: %d/%d ACCEPTED before trigger; submitting now",
                n_accepted, len(cell_results),
            )
        else:
            # Pre-open click refused (or timed out): fall back to the normal
            # at-trigger round. Retries below then work from this round, not
            # from the pre-open one.
            log.warning(
                "pre-click: nothing ACCEPTED before trigger (results: %s); "
                "re-clicking now",
                [cr.outcome.name for cr in cell_results],
            )
            cell_results = await _click_round()

    # Phase 1b: nothing ACCEPTED but some candidates merely timed out / 5xx'd -
    # re-fire those until one lands, none is transient any more, or the window
    # closes. Skipped as soon as anything is ACCEPTED: a ready submit must not
    # wait on a slow sibling.
    loop = asyncio.get_running_loop()
    retry_deadline = loop.time() + cell_retry_window_s
    attempt = 1
    refreshes = 0

    def _is_403(cr: CellClickResult) -> bool:
        return cr.outcome is CellOutcome.ERROR_FATAL and cr.http_status == 403

    while loop.time() < retry_deadline:
        if any(cr.outcome is CellOutcome.ACCEPTED for cr in cell_results):
            break
        # A 403 is re-clicked only after a token refresh, and only while the
        # refresh budget lasts; other ERROR_FATALs stay final.
        refresh = refreshes < CELL_403_REFRESH_MAX and any(
            _is_403(cr) for cr in cell_results
        )
        retry_ranks = [
            rank for rank, cr in enumerate(cell_results)
            if cr.outcome is CellOutcome.ERROR_TRANSIENT
            or (refresh and _is_403(cr))
        ]
        if not retry_ranks:
            break
        attempt += 1
        round_start = loop.time()
        if refresh:
            refreshes += 1
            n_403 = sum(_is_403(cr) for cr in cell_results)
            changed = await client.refresh_csrf_token(
                timeout=CELL_RETRY_TIMEOUT_SECONDS,
            )
            log.warning(
                "%d cell_click(s) answered 403; CSRFToken refresh %d/%d: %s",
                n_403, refreshes, CELL_403_REFRESH_MAX,
                {True: "token changed", False: "token unchanged",
                 None: "failed"}[changed],
            )
        log.warning(
            "no cell_click ACCEPTED, %d ERROR_TRANSIENT/403; retrying them "
            "(attempt %d, timeout=%.0fs)",
            len(retry_ranks), attempt, CELL_RETRY_TIMEOUT_SECONDS,
        )
        retried = await asyncio.gather(*(
            client.cell_click(candidates[rank], timeout=CELL_RETRY_TIMEOUT_SECONDS)
            for rank in retry_ranks
        ))
        for rank, cr in zip(retry_ranks, retried):
            cell_results[rank] = cr
            log.info(
                "attempt=%d rank=%d %s: cell=%s (latency=%dms)",
                attempt, rank, cr.slot.facility_name, cr.outcome.name, cr.latency_ms,
            )
        if not any(cr.outcome is CellOutcome.ACCEPTED for cr in retried):
            pause = cell_retry_min_round_s - (loop.time() - round_start)
            if pause > 0:
                await asyncio.sleep(pause)

    accepted = [
        (rank, cr.slot)
        for rank, cr in enumerate(cell_results)
        if cr.outcome is CellOutcome.ACCEPTED
    ]
    if not accepted:
        log.warning(
            "no cell_click ACCEPTED after %d attempt(s); (results: %s)",
            attempt, [cr.outcome.name for cr in cell_results],
        )
        return await _settle([], client, target_date, log)

    # Phase 2: group ACCEPTED by (start, end) preserving rank order.
    groups: "OrderedDict[tuple[datetime, datetime], list[tuple[int, AvailableSlot]]]" = OrderedDict()
    for rank, slot in accepted:
        groups.setdefault((slot.start_dt, slot.end_dt), []).append((rank, slot))

    # Submit retry state, shared by every candidate of this account.
    submit_deadline = loop.time() + submit_retry_window_s
    seen: set[BookingResult] = set()  # every submit answer so far
    token_gen = 0  # bumped by each successful submit-phase CSRFToken refresh
    submit_refreshes = 0
    refresh_lock = asyncio.Lock()

    def _retry_over() -> bool:
        return loop.time() >= submit_deadline or any(r in seen for r in _SUBMIT_FINAL)

    async def _refresh_token_once(gen_used: int) -> None:
        """Refresh the CSRFToken unless a sibling already has since the
        refused request was built, or the budget is spent."""
        nonlocal token_gen, submit_refreshes
        async with refresh_lock:
            if token_gen != gen_used or submit_refreshes >= SUBMIT_403_REFRESH_MAX:
                return
            submit_refreshes += 1
            changed = await client.refresh_csrf_token(
                timeout=CELL_RETRY_TIMEOUT_SECONDS,
            )
            if changed is not None:
                # A refresh that read no token is not a refresh: a sibling
                # holding the same generation may still try (budget allowing).
                token_gen += 1
            log.warning(
                "submit answered 403; CSRFToken refresh %d/%d: %s",
                submit_refreshes, SUBMIT_403_REFRESH_MAX,
                {True: "token changed", False: "token unchanged",
                 None: "failed"}[changed],
            )

    async def _submit(
        rank: int,
        slot: AvailableSlot,
        gen: int,
        first_answer: Callable[[BookingResult], None],
    ) -> BookingResult:
        """Submit one candidate; on 403 / 5xx / transport error re-click and
        re-submit it while the retry window is open (see SUBMIT_RETRY_*).

        `gen` is the token generation the first request is built with, and
        `first_answer` is told the first submit's result the moment it lands.
        The first submit goes out exactly as before; everything below it runs
        only after PolyU has answered without a verdict.
        """
        started = loop.time()
        result = await client.submit(slot)
        seen.add(result)
        first_answer(result)
        for retry in range(1, SUBMIT_RETRY_MAX + 1):
            if result not in _SUBMIT_RETRYABLE or _retry_over():
                break
            log.warning(
                "submit rank=%d %s answered %s; retry %d/%d (re-click, re-submit)",
                rank, slot.facility_name, result.name, retry, SUBMIT_RETRY_MAX,
            )
            if result is BookingResult.ERROR_FORBIDDEN:
                await _refresh_token_once(gen)
            # Let siblings report first, then keep this candidate's attempts
            # at least submit_retry_gap_s apart.
            await asyncio.sleep(0)
            if _retry_over():
                break
            pause = submit_retry_gap_s - (loop.time() - started)
            if pause > 0:
                await asyncio.sleep(pause)
                if _retry_over():
                    break
            gen = token_gen  # the re-click is built with this token
            started = loop.time()
            click = await client.cell_click(slot, timeout=CELL_RETRY_TIMEOUT_SECONDS)
            log.info(
                "submit retry rank=%d %s: cell=%s (latency=%dms)",
                rank, slot.facility_name, click.outcome.name, click.latency_ms,
            )
            if click.outcome is CellOutcome.OCCUPIED:
                result = BookingResult.OCCUPIED
                break
            if _is_403(click):
                result = BookingResult.ERROR_FORBIDDEN
                continue
            if click.outcome is CellOutcome.ERROR_TRANSIENT:
                result = BookingResult.ERROR_TRANSIENT
                continue
            if click.outcome is not CellOutcome.ACCEPTED or _retry_over():
                break
            # The click may have taken seconds and a sibling may have
            # refreshed meanwhile: the re-submit is built with the token of now.
            gen = token_gen
            result = await client.submit(slot)
            seen.add(result)
        return result

    async def _run_group(
        group: list[tuple[int, AvailableSlot]],
        first_round: "asyncio.Future[list[BookingResult]]",
    ) -> list[tuple[tuple[int, AvailableSlot], BookingResult]]:
        """Submit a group concurrently. `first_round` resolves as soon as every
        candidate's *first* submit has answered - the launch loop waits on
        that, never on the retries that may follow."""
        gen = token_gen
        firsts: list[BookingResult] = []

        def _first_answer(result: BookingResult) -> None:
            firsts.append(result)
            if len(firsts) == len(group) and not first_round.done():
                first_round.set_result(firsts)

        try:
            results = await asyncio.gather(
                *(_submit(rank, slot, gen, _first_answer) for rank, slot in group)
            )
        finally:
            if not first_round.done():
                # Crashed or cancelled: never leave the launch loop waiting.
                first_round.set_result(firsts)
        for (rank, slot), result in zip(group, results):
            log.info("submit rank=%d %s: %s", rank, slot.facility_name, result.name)
        return list(zip(group, results))

    launched: list[asyncio.Task] = []
    first_rounds: "list[asyncio.Future[list[BookingResult]]]" = []
    for (start_dt, _end_dt), group in groups.items():
        if launched:
            # Give the higher-priority groups a head start, but cap it: a hung
            # submit must not spend its whole timeout budget before the
            # fallback group is even dispatched. Returns early once every
            # launched group's first submits have answered, so a prompt answer
            # costs no delay - and neither does a group that is now retrying.
            await asyncio.wait(first_rounds, timeout=stagger_s)
            for task in launched:
                if task.done():
                    task.result()  # a real bug still crashes the run, with a traceback
            answered = {r for fr in first_rounds if fr.done() for r in fr.result()}
            if BookingResult.SUCCESS in seen:
                break  # Already booked — don't burn the daily quota on a fallback.
            if BookingResult.ERROR_FATAL in answered:
                # Either auth is dead or (since 2026-10) the booking went
                # through behind a "Failed to reserve facility" page; in
                # both cases a further group is wasted at best. A 403
                # (ERROR_FORBIDDEN) is neither, and does not stop the ladder:
                # on 2026-11-21 it kept student off 19:30 and 21:30.
                log.error(
                    "submit ERROR_FATAL with no SUCCESS before timeslot %s; "
                    "not launching further groups (checking records instead)",
                    start_dt.strftime("%H:%M"),
                )
                break
        first_round = loop.create_future()
        first_rounds.append(first_round)
        launched.append(asyncio.create_task(_run_group(group, first_round)))

    outcomes = [pair for chunk in await asyncio.gather(*launched) for pair in chunk]
    return await _settle(outcomes, client, target_date, log)


async def _settle(
    outcomes: list[tuple[tuple[int, AvailableSlot], BookingResult]],
    client: _ClientLike,
    target_date: date,
    log: logging.Logger,
) -> BookingOutcome:
    """Turn the submit results into the run's outcome, checked against
    PolyU's booking records (see `_recorded_booking`)."""
    # Groups overlap, so results do not arrive in priority order. Pick the
    # winner by rank (time-major, facility-minor) rather than by arrival.
    successes = sorted(
        (rank, slot) for (rank, slot), result in outcomes
        if result is BookingResult.SUCCESS
    )
    if successes:
        winner_rank, winner_slot = successes[0]
        log.info(
            "done: booked %s @ %s (rank=%d)",
            winner_slot.facility_name,
            winner_slot.start_dt.strftime("%H:%M"),
            winner_rank,
        )
        if len(successes) > 1:
            surplus = [(r, s.facility_name, s.start_dt.strftime("%H:%M"))
                       for r, s in successes[1:]]
            log.warning(
                "multi-SUCCESS across %d bookings: also booked %s — cancel manually",
                len(successes), surplus,
            )
        # A 302 -> make_book_result.do has never lied; the records are only a
        # cross-check here, so a mismatch warns instead of overriding it.
        recorded = await _recorded_booking(client, target_date, log)
        if recorded is None:
            log.warning(
                "booking records do not list %s yet; trusting the SUCCESS response",
                target_date,
            )
        elif recorded and recorded != winner_slot:
            log.warning(
                "booking records list %s @ %s for %s, not the SUCCESS slot %s @ %s",
                recorded.facility_name, recorded.start_dt.strftime("%H:%M"), target_date,
                winner_slot.facility_name, winner_slot.start_dt.strftime("%H:%M"),
            )
        return BookingOutcome(0, winner_slot)

    # No SUCCESS response. Since 2026-10 that does not mean no booking: ask
    # PolyU's records before reporting a loss.
    n_fatal = sum(r is BookingResult.ERROR_FATAL for _, r in outcomes)
    recorded = await _recorded_booking(client, target_date, log)
    if recorded:
        log.warning(
            "no SUCCESS response (%d submits, %d FATAL) but PolyU's records show "
            "%s @ %s booked for %s; counting it as booked",
            len(outcomes), n_fatal, recorded.facility_name,
            recorded.start_dt.strftime("%H:%M"), target_date,
        )
        return BookingOutcome(0, recorded, note=RECORDS_NOTE)

    if not outcomes:
        log.warning("no candidate reached submit; exiting 1")
    elif n_fatal:
        log.error(
            "submit ERROR_FATAL with no SUCCESS among %d submits and nothing in "
            "the records; exiting 1", len(outcomes),
        )
    else:
        log.warning("no submit succeeded among %d submits; exiting 1", len(outcomes))
    return BookingOutcome(1)
