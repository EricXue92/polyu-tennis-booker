"""HTTP-based booking orchestrator (parallel cell-clicks, time-grouped submits).

Phase 1: fire all (priority x facility) cell_click POSTs concurrently via
asyncio.gather. They finish in roughly first-POST latency, not stacked, so
the "first-POST 5-6s cold tax" no longer cascades onto candidates 2-4.

Phase 2: group ACCEPTED cells by their (start, end) time-slot, preserving rank
order. Within each group, fire submits concurrently via asyncio.gather; between
groups, walk sequentially in priority order. Any SUCCESS in a group wins (and
we report the lowest-rank SUCCESS if multiple). ERROR_FATAL aborts further
groups, but only if no SUCCESS in the same group (a parallel SUCCESS proves
auth is alive). OCCUPIED / ERROR_TRANSIENT advance to the next group.

Priority semantics: the user's preference is time-major, facility-minor
("any court at 18:30 before 19:30"). Parallel-within-group respects that:
18:30/court1 and 18:30/court2 race together; both 18:30 attempts finish
before 19:30 attempts start. The previous strict facility-priority within a
time-slot is sacrificed - a slow rank 0 submit (10s ReadTimeout on
2026-06-18) used to lock out the whole chain.

A multi-SUCCESS within one group is theoretically possible (two different
courts both committing for the same user at the same time) but unobserved
in production because contested slots are scarce. If it happens, we log a
WARNING with the surplus bookings so they can be cancelled manually.

Cell-click ERROR_TRANSIENT and ERROR_FATAL are tolerated at the candidate
level - a facility-specific failure does not poison sibling candidates.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import date, datetime, time
from typing import Protocol

from src.http_client import (
    AvailableSlot,
    BookingResult,
    CellClickResult,
    CellOutcome,
)


class _ClientLike(Protocol):
    """Minimal subset of PolyUHttpClient used by the orchestrator."""
    async def cell_click(self, slot: AvailableSlot) -> CellClickResult: ...
    async def submit(self, slot: AvailableSlot) -> BookingResult: ...


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


async def book_via_http(
    client: _ClientLike,
    target_date: date,
    slots: list[tuple[time, time]],
    dry_run: bool,
    *,
    log: logging.Logger,
) -> int:
    """Run the parallel cell-click + time-grouped submit flow.

    Returns 0 on SUCCESS, 1 otherwise.
    """
    candidates = _build_candidates(target_date, slots)
    log.info("predictive booking: %d candidates queued", len(candidates))

    if dry_run:
        log.info("DRY RUN: stopping before cell_click phase")
        return 0

    # Phase 1: parallel cell-clicks.
    # return_exceptions=False - cell_click catches httpx.HTTPError internally
    # and returns ERROR_TRANSIENT. Any uncaught exception is a real bug and
    # should crash the run with a traceback in CI, not be silently swallowed.
    cell_results: list[CellClickResult] = await asyncio.gather(
        *(client.cell_click(slot) for slot in candidates)
    )

    for rank, cr in enumerate(cell_results):
        log.info(
            "rank=%d %s: cell=%s (latency=%dms)",
            rank, cr.slot.facility_name, cr.outcome.name, cr.latency_ms,
        )

    accepted = [
        (rank, cr.slot)
        for rank, cr in enumerate(cell_results)
        if cr.outcome is CellOutcome.ACCEPTED
    ]
    if not accepted:
        log.warning(
            "no cell_click ACCEPTED; exiting 1 (results: %s)",
            [cr.outcome.name for cr in cell_results],
        )
        return 1

    # Phase 2: group ACCEPTED by (start, end) preserving rank order.
    groups: "OrderedDict[tuple[datetime, datetime], list[tuple[int, AvailableSlot]]]" = OrderedDict()
    for rank, slot in accepted:
        groups.setdefault((slot.start_dt, slot.end_dt), []).append((rank, slot))

    for (start_dt, _end_dt), group in groups.items():
        results = await asyncio.gather(
            *(client.submit(slot) for _, slot in group)
        )
        for (rank, slot), result in zip(group, results):
            log.info("submit rank=%d %s: %s", rank, slot.facility_name, result.name)

        # SUCCESS first: any SUCCESS in this group wins. If multiple, prefer
        # the lowest-rank one (group is already rank-ordered) and warn about
        # the surplus so the user can cancel manually.
        successes = [
            (rank, slot)
            for (rank, slot), result in zip(group, results)
            if result is BookingResult.SUCCESS
        ]
        if successes:
            winner_rank, winner_slot = successes[0]
            log.info(
                "done: booked %s @ %s (rank=%d)",
                winner_slot.facility_name,
                winner_slot.start_dt.strftime("%H:%M"),
                winner_rank,
            )
            if len(successes) > 1:
                surplus = [(r, s.facility_name) for r, s in successes[1:]]
                log.warning(
                    "multi-SUCCESS in timeslot %s: also booked %s — cancel manually",
                    start_dt.strftime("%H:%M"),
                    surplus,
                )
            return 0

        # No SUCCESS in this group. A FATAL with no SUCCESS sibling means
        # auth is presumably dead — further groups would hit the same wall.
        if any(r is BookingResult.ERROR_FATAL for r in results):
            log.error(
                "submit ERROR_FATAL with no SUCCESS in timeslot %s; "
                "aborting remaining groups (auth presumed dead)",
                start_dt.strftime("%H:%M"),
            )
            return 1
        # OCCUPIED / ERROR_TRANSIENT only -> try next timeslot group.

    log.warning("no submit succeeded among %d ACCEPTED candidates; exiting 1", len(accepted))
    return 1
