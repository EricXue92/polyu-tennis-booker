"""Offline tests for book_via_http orchestrator using a fake PolyUHttpClient."""
from datetime import date, datetime, time
import logging

import pytest

from src.http_client import AvailableSlot, BookingResult


class _FakeClient:
    """Minimal duck-typed stand-in for PolyUHttpClient.

    Records every try_book call, returns canned results per call.
    """

    def __init__(self, availability, try_book_results):
        self._availability = availability
        self._try_book_results = list(try_book_results)
        self.search_calls = 0
        self.try_book_calls = []

    async def search(self, target_date):
        self.search_calls += 1
        return self._availability

    async def try_book(self, slot):
        self.try_book_calls.append(slot)
        return self._try_book_results.pop(0)


def _slot(facility_id, hour):
    return AvailableSlot(
        facility_id=facility_id,
        facility_name=f"Tennis Court No. {facility_id - 9}",
        center_id=1,
        center_name="Shaw Sports Complex",
        start_dt=datetime(2026, 6, 10, hour, 30),
        end_dt=datetime(2026, 6, 10, hour + 1, 30),
    )


_LOG = logging.getLogger("test")


@pytest.mark.asyncio
async def test_book_via_http_returns_0_on_first_success():
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={
            (time(17, 30), time(18, 30)): [_slot(11, 17)],
            (time(18, 30), time(19, 30)): [_slot(10, 18), _slot(11, 18)],
            (time(19, 30), time(20, 30)): [_slot(11, 19)],
        },
        try_book_results=[BookingResult.SUCCESS],  # first attempt wins
    )
    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30)), (time(17, 30), time(18, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 0
    assert client.search_calls == 1
    # Rank 0 (18:30-19:30) tried first; OCCUPIED would advance — but SUCCESS short-circuits.
    assert len(client.try_book_calls) == 1
    assert client.try_book_calls[0].start_dt == datetime(2026, 6, 10, 18, 30)


@pytest.mark.asyncio
async def test_book_via_http_advances_through_occupied():
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={
            (time(17, 30), time(18, 30)): [_slot(11, 17)],
            (time(18, 30), time(19, 30)): [_slot(11, 18)],
            (time(19, 30), time(20, 30)): [_slot(11, 19)],
        },
        try_book_results=[BookingResult.OCCUPIED, BookingResult.OCCUPIED, BookingResult.SUCCESS],
    )
    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30)), (time(17, 30), time(18, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 0
    # All three ranks attempted in priority order.
    assert [s.start_dt.hour for s in client.try_book_calls] == [18, 19, 17]


@pytest.mark.asyncio
async def test_book_via_http_returns_1_when_all_occupied():
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={
            (time(17, 30), time(18, 30)): [_slot(11, 17)],
            (time(18, 30), time(19, 30)): [_slot(11, 18)],
        },
        try_book_results=[BookingResult.OCCUPIED, BookingResult.OCCUPIED],
    )
    priority = [(time(18, 30), time(19, 30)), (time(17, 30), time(18, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 1


@pytest.mark.asyncio
async def test_book_via_http_skips_priority_with_no_free_facility():
    # If rank 0 has no free facility, don't try_book — advance to rank 1.
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={
            # rank 0 missing from availability
            (time(19, 30), time(20, 30)): [_slot(11, 19)],
        },
        try_book_results=[BookingResult.SUCCESS],
    )
    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.try_book_calls) == 1
    assert client.try_book_calls[0].start_dt.hour == 19  # rank 1, not rank 0


@pytest.mark.asyncio
async def test_book_via_http_aborts_on_error():
    # ERROR from try_book means session is broken (auth lost, 500). Don't
    # burn through remaining priorities — return 1 so the watchdog opens an
    # issue and we can investigate.
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={
            (time(17, 30), time(18, 30)): [_slot(11, 17)],
            (time(18, 30), time(19, 30)): [_slot(11, 18)],
        },
        try_book_results=[BookingResult.ERROR],
    )
    priority = [(time(18, 30), time(19, 30)), (time(17, 30), time(18, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 1
    # Only rank 0 attempted — ERROR aborts before rank 1.
    assert len(client.try_book_calls) == 1


@pytest.mark.asyncio
async def test_book_via_http_dry_run_does_not_call_try_book():
    from src.http_booker import book_via_http

    client = _FakeClient(
        availability={(time(18, 30), time(19, 30)): [_slot(11, 18)]},
        try_book_results=[],  # would explode if try_book is called
    )
    priority = [(time(18, 30), time(19, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=True, log=_LOG)
    assert rc == 0
    assert client.try_book_calls == []
