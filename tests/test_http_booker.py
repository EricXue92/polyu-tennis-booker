"""Offline tests for the parallel-cell-click book_via_http orchestrator.

book_via_http:
  1. Constructs candidates from (slots × TENNIS_FACILITIES), priority-major.
  2. Fires all cell_click POSTs concurrently via asyncio.gather.
  3. Walks results in priority order; for each ACCEPTED, calls submit.
     - SUCCESS         -> return 0
     - OCCUPIED        -> advance to next ACCEPTED
     - ERROR_TRANSIENT -> advance to next ACCEPTED
     - ERROR_FATAL     -> abort remaining submits, return 1
  4. If 0 ACCEPTED, return 1 (no submit calls).
"""
import asyncio
import logging
import time
from datetime import date, time as dtime

import pytest

from src.config import TENNIS_FACILITIES
from src.http_client import BookingResult, CellClickResult, CellOutcome


class _FakeClient:
    """Scripted client: per-candidate cell_click outcome + per-candidate submit result.

    cell_click_outcomes / submit_results are dicts keyed by (start_hour, facility_id)
    so tests can express intent without depending on candidate construction order.
    """
    def __init__(
        self,
        cell_click_outcomes: dict[tuple[int, int], CellOutcome],
        submit_results: dict[tuple[int, int], BookingResult] | None = None,
        cell_click_sleep_s: float = 0.0,
    ):
        self._cell = cell_click_outcomes
        self._sub = submit_results or {}
        self._sleep = cell_click_sleep_s
        self.cell_click_calls = []
        self.submit_calls = []

    async def cell_click(self, slot):
        self.cell_click_calls.append(slot)
        if self._sleep:
            await asyncio.sleep(self._sleep)
        key = (slot.start_dt.hour, slot.facility_id)
        outcome = self._cell[key]
        return CellClickResult(slot=slot, outcome=outcome, latency_ms=10)

    async def submit(self, slot):
        self.submit_calls.append(slot)
        key = (slot.start_dt.hour, slot.facility_id)
        return self._sub[key]


_LOG = logging.getLogger("test")
_FACILITY_IDS = list(TENNIS_FACILITIES.keys())  # [10, 11]
assert len(_FACILITY_IDS) == 2, "tests assume exactly 2 tennis facilities"
_PRIORITY = [(dtime(18, 30), dtime(19, 30)), (dtime(19, 30), dtime(20, 30))]


def _all_cell(outcome: CellOutcome) -> dict[tuple[int, int], CellOutcome]:
    return {(h, f): outcome for h in (18, 19) for f in _FACILITY_IDS}


def _all_submit(result: BookingResult) -> dict[tuple[int, int], BookingResult]:
    return {(h, f): result for h in (18, 19) for f in _FACILITY_IDS}


@pytest.mark.asyncio
async def test_happy_path_rank0_wins():
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.cell_click_calls) == 4
    assert len(client.submit_calls) == 1
    assert (client.submit_calls[0].start_dt.hour, client.submit_calls[0].facility_id) == (18, _FACILITY_IDS[0])


@pytest.mark.asyncio
async def test_priority_preserved_when_only_some_accepted():
    # Rank 0 (18:30 court A) and rank 2 (19:30 court A) ACCEPTED;
    # rank 1, 3 OCCUPIED. Submit must hit rank 0, not rank 2.
    from src.http_booker import book_via_http

    cells = _all_cell(CellOutcome.OCCUPIED)
    cells[(18, _FACILITY_IDS[0])] = CellOutcome.ACCEPTED
    cells[(19, _FACILITY_IDS[0])] = CellOutcome.ACCEPTED

    client = _FakeClient(
        cell_click_outcomes=cells,
        submit_results={(18, _FACILITY_IDS[0]): BookingResult.SUCCESS,
                        (19, _FACILITY_IDS[0]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 1
    assert client.submit_calls[0].start_dt.hour == 18


@pytest.mark.asyncio
async def test_fallback_to_rank1_after_rank0_submit_occupied():
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[1]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 2
    assert client.submit_calls[0].facility_id == _FACILITY_IDS[0]  # rank 0
    assert client.submit_calls[1].facility_id == _FACILITY_IDS[1]  # rank 1


@pytest.mark.asyncio
async def test_all_occupied_in_cell_phase_returns_1_with_no_submits():
    from src.http_booker import book_via_http

    client = _FakeClient(cell_click_outcomes=_all_cell(CellOutcome.OCCUPIED))
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.cell_click_calls) == 4
    assert len(client.submit_calls) == 0


@pytest.mark.asyncio
async def test_all_accepted_all_submit_occupied_returns_1():
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results=_all_submit(BookingResult.OCCUPIED),
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.submit_calls) == 4


@pytest.mark.asyncio
async def test_cell_transient_does_not_block_other_candidates():
    from src.http_booker import book_via_http

    cells = _all_cell(CellOutcome.ACCEPTED)
    cells[(19, _FACILITY_IDS[0])] = CellOutcome.ERROR_TRANSIENT
    client = _FakeClient(
        cell_click_outcomes=cells,
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 1
    assert client.submit_calls[0].start_dt.hour == 18


@pytest.mark.asyncio
async def test_cell_fatal_does_not_abort_globally():
    # rank 0 cell FATAL; rank 1 ACCEPTED -> SUCCESS. Cell FATAL is per-candidate.
    from src.http_booker import book_via_http

    cells = _all_cell(CellOutcome.ACCEPTED)
    cells[(18, _FACILITY_IDS[0])] = CellOutcome.ERROR_FATAL
    client = _FakeClient(
        cell_click_outcomes=cells,
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[1]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 1
    assert client.submit_calls[0].facility_id == _FACILITY_IDS[1]


@pytest.mark.asyncio
async def test_submit_fatal_aborts_remaining_submits():
    # All ACCEPTED, but rank 0 submit returns FATAL. Don't try rank 1/2/3.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.SUCCESS),
                        (18, _FACILITY_IDS[0]): BookingResult.ERROR_FATAL},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.submit_calls) == 1


@pytest.mark.asyncio
async def test_submit_transient_continues_to_next_rank():
    # rank 0 submit TRANSIENT, rank 1 submit SUCCESS.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.ERROR_TRANSIENT,
                        (18, _FACILITY_IDS[1]): BookingResult.SUCCESS},
    )
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.submit_calls) == 2


@pytest.mark.asyncio
async def test_all_cell_errors_returns_1():
    from src.http_booker import book_via_http

    cells = _all_cell(CellOutcome.ERROR_TRANSIENT)
    cells[(18, _FACILITY_IDS[1])] = CellOutcome.ERROR_FATAL
    client = _FakeClient(cell_click_outcomes=cells)
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.submit_calls) == 0


@pytest.mark.asyncio
async def test_cell_clicks_actually_run_in_parallel():
    # Each cell_click sleeps 500ms. If serial, total > 2.0s; if parallel, < 1.2s.
    # Use 1.2s threshold to absorb CI scheduler jitter.
    from src.http_booker import book_via_http

    client = _FakeClient(
        cell_click_outcomes=_all_cell(CellOutcome.ACCEPTED),
        submit_results={**_all_submit(BookingResult.OCCUPIED),
                        (18, _FACILITY_IDS[0]): BookingResult.SUCCESS},
        cell_click_sleep_s=0.5,
    )
    t0 = time.perf_counter()
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=False, log=_LOG)
    elapsed = time.perf_counter() - t0
    assert rc == 0
    assert elapsed < 1.2, f"cell_clicks ran sequentially (took {elapsed:.2f}s, expected < 1.2s)"


@pytest.mark.asyncio
async def test_dry_run_does_not_call_cell_click_or_submit():
    from src.http_booker import book_via_http

    # Use sparse outcomes - KeyError would fire if cell_click was actually called.
    client = _FakeClient(cell_click_outcomes={})
    rc = await book_via_http(client, date(2026, 6, 10), _PRIORITY, dry_run=True, log=_LOG)
    assert rc == 0
    assert client.cell_click_calls == []
    assert client.submit_calls == []
