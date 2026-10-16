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
    wait_for_trigger: TriggerWait | None = None,
) -> int:
    """Run the booking flow. Returns 0 on SUCCESS, 1 otherwise."""
    outcome = await book_via_http_outcome(
        client, target_date, slots, dry_run, log=log, stagger_s=stagger_s,
        cell_retry_window_s=cell_retry_window_s,
        cell_retry_min_round_s=cell_retry_min_round_s,
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

    async def _click_round() -> list[CellClickResult]:
        # Parallel cell-clicks.
        # return_exceptions=False - cell_click catches httpx.HTTPError
        # internally and returns ERROR_TRANSIENT. Any uncaught exception is a
        # real bug and should crash the run with a traceback in CI, not be
        # silently swallowed.
        results: list[CellClickResult] = list(await asyncio.gather(
            *(client.cell_click(slot) for slot in candidates)
        ))
        for rank, cr in enumerate(results):
            log.info(
                "rank=%d %s: cell=%s (latency=%dms)",
                rank, cr.slot.facility_name, cr.outcome.name, cr.latency_ms,
            )
        return results

    # Phase 1: cell-click round (pre-click when a trigger is supplied).
    cell_results = await _click_round()

    if wait_for_trigger is not None:
        n_accepted = sum(cr.outcome is CellOutcome.ACCEPTED for cr in cell_results)
        await wait_for_trigger()
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
    while loop.time() < retry_deadline:
        if any(cr.outcome is CellOutcome.ACCEPTED for cr in cell_results):
            break
        retry_ranks = [
            rank for rank, cr in enumerate(cell_results)
            if cr.outcome is CellOutcome.ERROR_TRANSIENT
        ]
        if not retry_ranks:
            break
        attempt += 1
        log.warning(
            "no cell_click ACCEPTED, %d ERROR_TRANSIENT; retrying them "
            "(attempt %d, timeout=%.0fs)",
            len(retry_ranks), attempt, CELL_RETRY_TIMEOUT_SECONDS,
        )
        round_start = loop.time()
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

    async def _run_group(
        group: list[tuple[int, AvailableSlot]],
    ) -> list[tuple[tuple[int, AvailableSlot], BookingResult]]:
        results = await asyncio.gather(
            *(client.submit(slot) for _, slot in group)
        )
        for (rank, slot), result in zip(group, results):
            log.info("submit rank=%d %s: %s", rank, slot.facility_name, result.name)
        return list(zip(group, results))

    def _settled() -> list[tuple[tuple[int, AvailableSlot], BookingResult]]:
        """Results from groups that have already finished. Re-raises task
        exceptions so a real bug still crashes the run with a traceback."""
        out: list[tuple[tuple[int, AvailableSlot], BookingResult]] = []
        for task in launched:
            if task.done():
                out.extend(task.result())
        return out

    launched: list[asyncio.Task] = []
    for (start_dt, _end_dt), group in groups.items():
        if launched:
            # Give the higher-priority groups a head start, but cap it: a hung
            # submit must not spend its whole timeout budget before the
            # fallback group is even dispatched. Returns early once every
            # launched group has settled, so a prompt answer costs no delay.
            await asyncio.wait(launched, timeout=stagger_s)
            settled = _settled()
            if any(r is BookingResult.SUCCESS for _, r in settled):
                break  # Already booked — don't burn the daily quota on a fallback.
            if any(r is BookingResult.ERROR_FATAL for _, r in settled):
                # Either auth is dead or (since 2026-10) the booking went
                # through behind a "Failed to reserve facility" page; in
                # both cases a further group is wasted at best.
                log.error(
                    "submit ERROR_FATAL with no SUCCESS before timeslot %s; "
                    "not launching further groups (checking records instead)",
                    start_dt.strftime("%H:%M"),
                )
                break
        launched.append(asyncio.create_task(_run_group(group)))

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
