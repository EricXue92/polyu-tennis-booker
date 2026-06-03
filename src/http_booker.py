"""HTTP-based booking orchestrator.

Drives a PolyUHttpClient through the 08:30 HKT booking flow:
 1. Search the target date.
 2. For each priority (start, end) slot, in rank order:
    a. Pick a free facility (any) from the search results.
    b. Call client.try_book(slot).
    c. SUCCESS → return 0. OCCUPIED → advance to next rank. ERROR → abort.
 3. If no rank produces SUCCESS, return 1.

Replaces the old parallel_runner.py + single-dequeuer coordinator design.
With HTTP, attempts are cheap and serial — no need for N concurrent
sessions racing for the same date.
"""
from __future__ import annotations

import logging
from datetime import date, time
from typing import Protocol

from src.http_client import AvailableSlot, BookingResult


class _ClientLike(Protocol):
    """Minimal subset of PolyUHttpClient used by the orchestrator.

    Defined as a Protocol so tests can pass a fake client without
    inheriting from PolyUHttpClient.
    """
    async def search(
        self,
        target_date: date,
    ) -> dict[tuple[time, time], list[AvailableSlot]]: ...

    async def try_book(self, slot: AvailableSlot) -> BookingResult: ...


async def book_via_http(
    client: _ClientLike,
    target_date: date,
    slots: list[tuple[time, time]],
    dry_run: bool,
    *,
    log: logging.Logger,
) -> int:
    """Search, then attempt priority slots serially. Returns 0 on SUCCESS, 1 otherwise."""
    log.info("calling search for %s", target_date)
    availability = await client.search(target_date)
    log.info("search returned %d free time-slots", len(availability))

    for rank, (start, end) in enumerate(slots):
        free = availability.get((start, end), [])
        if not free:
            log.info("rank=%d %s-%s: no free facility, skipping", rank, start, end)
            continue
        slot = free[0]  # any free facility for this time will do
        log.info(
            "rank=%d trying %s on %s (facility=%d)",
            rank, slot.facility_name, slot.start_dt, slot.facility_id,
        )
        if dry_run:
            log.info("DRY RUN: stopping before try_book")
            return 0
        result = await client.try_book(slot)
        log.info("rank=%d %s: result=%s", rank, slot.facility_name, result.name)
        if result is BookingResult.SUCCESS:
            return 0
        if result is BookingResult.ERROR:
            log.error("try_book returned ERROR; aborting to avoid burning priorities on a broken session")
            return 1
        # OCCUPIED → fall through to the next rank.

    log.warning("no priority slot succeeded; exiting with 1")
    return 1
