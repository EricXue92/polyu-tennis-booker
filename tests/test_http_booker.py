"""Offline tests for book_via_http orchestrator using a fake PolyUHttpClient.

book_via_http skips search and constructs candidates directly from
(SLOT_PRIORITY × TENNIS_FACILITIES). Tests pin the candidate order
(priority-major, facility-minor) and the SUCCESS/OCCUPIED/ERROR/dry-run
semantics.
"""
from datetime import date, time
import logging

import pytest

from src.config import TENNIS_FACILITIES
from src.http_client import BookingResult


class _FakeClient:
    """Records every try_book call, returns canned results per call."""

    def __init__(self, try_book_results):
        self._try_book_results = list(try_book_results)
        self.try_book_calls = []

    async def try_book(self, slot):
        self.try_book_calls.append(slot)
        return self._try_book_results.pop(0)


_LOG = logging.getLogger("test")
_FACILITY_IDS = list(TENNIS_FACILITIES.keys())  # [10, 11]
assert len(_FACILITY_IDS) == 2, "tests assume exactly 2 tennis facilities"


@pytest.mark.asyncio
async def test_book_via_http_returns_0_on_first_success():
    from src.http_booker import book_via_http

    client = _FakeClient(try_book_results=[BookingResult.SUCCESS])
    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.try_book_calls) == 1
    # First candidate: priority rank 0 (18:30), first facility in TENNIS_FACILITIES.
    first = client.try_book_calls[0]
    assert first.start_dt.hour == 18 and first.start_dt.minute == 30
    assert first.facility_id == _FACILITY_IDS[0]


@pytest.mark.asyncio
async def test_book_via_http_iterates_priority_major_facility_minor():
    # Priority-major: try ALL facilities for rank 0 before advancing to rank 1.
    # User intent: "I want 18:30, any court, before I'll accept 19:30."
    from src.http_booker import book_via_http

    client = _FakeClient(try_book_results=[
        BookingResult.OCCUPIED,  # rank 0 facility 0
        BookingResult.OCCUPIED,  # rank 0 facility 1
        BookingResult.SUCCESS,   # rank 1 facility 0
    ])
    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 0
    assert [(s.start_dt.hour, s.facility_id) for s in client.try_book_calls] == [
        (18, _FACILITY_IDS[0]),
        (18, _FACILITY_IDS[1]),
        (19, _FACILITY_IDS[0]),
    ]


@pytest.mark.asyncio
async def test_book_via_http_returns_1_when_all_occupied():
    from src.http_booker import book_via_http

    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30))]
    n_candidates = len(priority) * len(_FACILITY_IDS)
    client = _FakeClient(try_book_results=[BookingResult.OCCUPIED] * n_candidates)
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.try_book_calls) == n_candidates


@pytest.mark.asyncio
async def test_book_via_http_aborts_on_fatal_error():
    # ERROR_FATAL means session is dead (4xx, unrecognised response). Don't
    # burn through remaining candidates — return 1 so the watchdog opens an issue.
    from src.http_booker import book_via_http

    client = _FakeClient(try_book_results=[BookingResult.ERROR_FATAL])
    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.try_book_calls) == 1


@pytest.mark.asyncio
async def test_book_via_http_continues_on_transient_error():
    # ERROR_TRANSIENT (5xx, network) on the first candidate must not block the
    # rest — 2026-06-06 lost a slot because a single 6.6s ERROR aborted the run
    # before Court No. 2 was even tried.
    from src.http_booker import book_via_http

    client = _FakeClient(try_book_results=[
        BookingResult.ERROR_TRANSIENT,  # rank 0 facility 0 — transient hiccup
        BookingResult.SUCCESS,           # rank 0 facility 1
    ])
    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 0
    assert len(client.try_book_calls) == 2


@pytest.mark.asyncio
async def test_book_via_http_returns_1_when_all_transient_errors():
    # If every candidate hits a transient error we still exit 1 so the
    # watchdog issue fires — but we tried them all instead of aborting after
    # the first.
    from src.http_booker import book_via_http

    priority = [(time(18, 30), time(19, 30)), (time(19, 30), time(20, 30))]
    n_candidates = len(priority) * len(_FACILITY_IDS)
    client = _FakeClient(try_book_results=[BookingResult.ERROR_TRANSIENT] * n_candidates)
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=False, log=_LOG)
    assert rc == 1
    assert len(client.try_book_calls) == n_candidates


@pytest.mark.asyncio
async def test_book_via_http_dry_run_does_not_call_try_book():
    from src.http_booker import book_via_http

    client = _FakeClient(try_book_results=[])  # would explode if try_book called
    priority = [(time(18, 30), time(19, 30))]
    rc = await book_via_http(client, date(2026, 6, 10), priority, dry_run=True, log=_LOG)
    assert rc == 0
    assert client.try_book_calls == []
