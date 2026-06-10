"""HTTP-based booking orchestrator (parallel cell-clicks).

Phase 1: fire all (priority x facility) cell_click POSTs concurrently via
asyncio.gather. They finish in roughly first-POST latency, not stacked, so
the "first-POST 5-6s cold tax" no longer cascades onto candidates 2-4.

Phase 2: walk cell_click results in priority order. For each ACCEPTED slot,
call submit serially. SUCCESS returns 0 immediately. OCCUPIED or
ERROR_TRANSIENT advances to the next ACCEPTED candidate. ERROR_FATAL aborts
remaining submits (auth presumed dead - further submits will hit the same
wall).

Strict priority guarantee: submit always runs sequentially in priority order.
It is impossible to book rank 3 when rank 0 also succeeded.

Cell-click ERROR_TRANSIENT and ERROR_FATAL are tolerated at the candidate
level - a facility-specific failure does not poison sibling candidates.
"""
from __future__ import annotations

import asyncio
import logging
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
    """Run the parallel cell-click + priority-ordered submit flow.

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

    # Phase 2: sequential submit, strict priority order.
    for rank, slot in accepted:
        result = await client.submit(slot)
        log.info("submit rank=%d %s: %s", rank, slot.facility_name, result.name)
        if result is BookingResult.SUCCESS:
            log.info(
                "done: booked %s @ %s (rank=%d)",
                slot.facility_name, slot.start_dt.strftime("%H:%M"), rank,
            )
            return 0
        if result is BookingResult.ERROR_FATAL:
            log.error("submit ERROR_FATAL; aborting remaining submits (auth presumed dead)")
            return 1
        # OCCUPIED or ERROR_TRANSIENT -> try next ACCEPTED candidate.

    log.warning("no submit succeeded among %d ACCEPTED candidates; exiting 1", len(accepted))
    return 1
