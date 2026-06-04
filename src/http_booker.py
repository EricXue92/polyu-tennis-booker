"""HTTP-based booking orchestrator (predictive).

At 08:30:00.000 HKT, PolyU's Search endpoint takes ~4.5s to respond. Other
users (or their bots) win popular slots inside that window. So we skip
search entirely: we already know the facility IDs (TENNIS_FACILITIES) and
target times (SLOT_PRIORITY), so we go straight to make_book.do for each
(priority, facility) pair in rank order. If the slot is actually free, the
cell-click + submit succeed. If it's already booked, the cell-click returns
OCCUPIED and we advance to the next candidate.

Trade-offs vs the old search-then-book flow:
 - Wins ~4.5s — first make_book.do hits the server at ~08:30:00.050 instead
   of ~08:30:04.500. That's the difference between getting popular slots
   and not.
 - We attempt facility IDs that may not exist on the target date (e.g. if
   PolyU adds/removes a court). Those return OCCUPIED via the same code
   path as a truly-occupied slot, so semantics don't change — we just
   waste one POST per nonexistent facility.
 - No "no free facility, skipping" log line — every priority gets every
   facility tried.

Order: priority-major, facility-minor. For SLOT_PRIORITY=[(18:30, 19:30),
(19:30, 20:30)] and TENNIS_FACILITIES={10, 11}, candidates are:
  rank 0: 18:30 court 10
  rank 1: 18:30 court 11
  rank 2: 19:30 court 10
  rank 3: 19:30 court 11
User intent: "I want 18:30, any court, before I'll accept 19:30."
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Protocol

from src.http_client import AvailableSlot, BookingResult


class _ClientLike(Protocol):
    """Minimal subset of PolyUHttpClient used by the orchestrator."""
    async def try_book(self, slot: AvailableSlot) -> BookingResult: ...


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
    """Attempt priority×facility candidates serially. Returns 0 on SUCCESS, 1 otherwise."""
    candidates = _build_candidates(target_date, slots)
    log.info("predictive booking: %d candidates queued", len(candidates))

    for rank, slot in enumerate(candidates):
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
            log.error("try_book returned ERROR; aborting to avoid burning candidates on a broken session")
            return 1
        # OCCUPIED → fall through to the next candidate.

    log.warning("no candidate succeeded; exiting with 1")
    return 1
